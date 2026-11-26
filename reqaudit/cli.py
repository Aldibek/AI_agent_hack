"""Командная строка: python -m reqaudit <файл требований> <файл отчёта>"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(root / ".env")

    p = argparse.ArgumentParser(
        prog="reqaudit",
        description="Статическая проверка требований по ISO/IEC/IEEE 29148 и IEEE 830. Пишет отчёт на русском.",
    )
    p.add_argument("input", help="путь к документу требований (.pdf, .docx, .md, .txt)")
    p.add_argument("output", help="путь к файлу отчёта (.md)")
    p.add_argument("--no-llm", action="store_true", help="только детерминированные правила, без вызовов LLM")
    p.add_argument("--no-cache", action="store_true", help="не использовать кэш ответов LLM")
    p.add_argument("--interactive", action="store_true", help="задать главные вопросы в терминале и перепроверить")
    p.add_argument("--force-budget", action="store_true",
                   help="начать прогон, даже если предварительная оценка стоимости выше REQAUDIT_BUDGET_USD")
    args = p.parse_args(argv)

    from .costs import BudgetExceeded
    from .pipeline import run
    from .report import render, write_source, write_trace

    out = Path(args.output).expanduser()
    if out.suffix.lower() not in (".md", ".markdown"):
        out = out.with_suffix(".md")
    cache = None if args.no_cache else root / ".reqaudit_cache"
    try:
        result = run(args.input, use_llm=not args.no_llm, cache_dir=cache, force_budget=args.force_budget)
    except BudgetExceeded as e:
        print(f"Бюджет: {e}", file=sys.stderr)
        return 3
    except (FileNotFoundError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2

    if args.interactive:
        from .clarify import apply_answers

        apply_answers(result, cache_dir=cache)

    render(result, out)
    write_trace(result, out.with_suffix(".json"))
    write_source(result, out.with_suffix(".source.txt"))
    from .report_html import render_html

    try:  # PDF — до HTML, чтобы HTML мог встроить его в кнопку «Скачать PDF»
        from .report_pdf import render_pdf

        render_pdf(result, out.with_suffix(".pdf"))
    except Exception as e:  # noqa: BLE001 — PDF вспомогательный, отчёт .md уже записан
        print(f"Внимание: PDF не создан ({e})", file=sys.stderr)
    render_html(result, out.with_suffix(".html"))
    print(f"Отчёт: {out} (также .pdf и .html рядом)", file=sys.stderr)
    print(f"Трассировка: {out.with_suffix('.json')}; текст документа с номерами строк: {out.with_suffix('.source.txt')}",
          file=sys.stderr)
    if result.budget_stop:
        print(f"Внимание: {result.budget_stop} Отчёт построен по выполненным шагам.", file=sys.stderr)
        return 3
    if result.budget_blocked:
        print("Внимание: часть шагов пропущена по бюджету прогона: " + "; ".join(result.budget_blocked), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
