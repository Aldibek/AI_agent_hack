"""Тесты детерминированной части: нарезка, правила, проверка цитат, склейка, разбор JSON.

Запуск: python -m unittest discover -s tests -v
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from reqaudit.grounding import Grounder
from reqaudit.ingest import load_document
from reqaudit.llm import parse_json
from reqaudit.models import Finding, Quote
from reqaudit.pipeline import merge_findings
from reqaudit.rules import run_rules
from reqaudit.segment import segment

SPEC = textwrap.dedent("""\
    # Тестовое ТЗ

    ## Цели
    Клиент быстро узнаёт о покупке.

    ## Функциональные требования
    * **Ф-1.** Клиент задаёт порог от 1 000 до 50 000 ₸ включительно.
    * **Ф-2.** Месяц определяется по дате покупки в Алматы.

    ## Требования к данным
    Хранить идентификатор карты и порог в тенге.

    ## Критерии приемки

    | Исходные условия | Действие | Ожидаемый результат |
    | :--- | :--- | :--- |
    | Расходы 10 000 ₸, порог 20 000 ₸ | Провести покупку на 5 000 ₸ | Итог 15 000 ₸ |
    | Расходы 10 000 ₸, порог 20 000 ₸ | Провести покупку на 5 000 ₸ | Итог 16 000 ₸ |

    ## REST API

    ### Общие параметры

    | Поле | Значение |
    | :--- | :--- |
    | **Description** | Сохранение порога |
    | **REST type** | GET |
    | **Swagger** | — |

    ### Параметры

    | Parameter | Format | Description | DB field |
    | :--- | :--- | :--- | :--- |
    | `cardId` | string | Идентификатор карты | `card_id` |
    | `enabled` | integer | Признак включения | `enabled` |

    #### 200. Successful — Created
    ```json
    {"result":"OK"}
    ```
    """)


class CoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        p = Path(cls.tmp.name) / "spec.md"
        p.write_text(SPEC, encoding="utf-8")
        cls.doc = load_document(p)
        cls.reqs = segment(cls.doc)
        cls.findings, cls.notes = run_rules(cls.doc, cls.reqs)
        g = Grounder(cls.doc, cls.reqs)
        cls.grounded = [f for f in cls.findings if g.ground(f)]
        cls.titles = " | ".join(f.title for f in cls.grounded)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_segment_ids_and_kinds(self):
        refs = [r.doc_ref for r in self.reqs if r.kind == "FR"]
        self.assertEqual(refs, ["Ф-1", "Ф-2"])
        self.assertEqual(sum(1 for r in self.reqs if r.kind == "AC"), 2)
        self.assertTrue(all(r.rid.startswith("R-") for r in self.reqs))

    def test_rules_find_known_defects(self):
        self.assertIn("GET", self.titles)
        self.assertIn("Created", self.titles)
        self.assertIn("границ", self.titles)
        self.assertIn("быстро", self.titles)
        self.assertIn("Часовой пояс", self.titles)
        self.assertIn("Признак включения", self.titles)
        self.assertIn("Swagger", self.titles)

    def test_ac_arithmetic(self):
        wrong = [f for f in self.grounded if f.source == "rule:ac_arithmetic"]
        self.assertEqual(len(wrong), 1, "ошибочная строка КП найдена ровно одна")
        self.assertTrue(any("верна" in n for n in self.notes), "верная строка КП отмечена как проверенная")

    def test_every_rule_finding_is_grounded(self):
        for f in self.grounded:
            self.assertTrue(f.quotes or f.search_scope, f.title)
            for q in f.quotes:
                self.assertTrue(q.grounded)
                self.assertIn(q.text.split()[0].strip("«»*`").lower()[:4], self.doc.lines[q.line_start - 1].text.lower())

    def test_fake_quote_is_dropped(self):
        g = Grounder(self.doc, self.reqs)
        f = Finding(source="llm:x", category="ambiguity", title="t",
                    quotes=[Quote(text="система должна работать круглосуточно без перерывов", line=5)])
        self.assertFalse(g.ground(f))

    def test_real_quote_gets_true_line(self):
        g = Grounder(self.doc, self.reqs)
        f = Finding(source="llm:x", category="ambiguity", title="t",
                    quotes=[Quote(text="Месяц определяется по дате покупки", line=1)])
        self.assertTrue(g.ground(f))
        self.assertEqual(self.doc.lines[f.quotes[0].line_start - 1].text.count("Ф-2"), 1)

    def test_merge_votes(self):
        g = Grounder(self.doc, self.reqs)
        a = Finding(source="llm:a", category="contradiction", title="Ф-1 против КП", votes=["a"],
                    quotes=[Quote(text="Клиент задаёт порог от 1 000 до 50 000"), Quote(text="Итог 16 000")])
        b = Finding(source="llm:b", category="contradiction", title="КП противоречит Ф-1", votes=["b"],
                    quotes=[Quote(text="Клиент задаёт порог от 1 000 до 50 000"), Quote(text="Итог 16 000")])
        for f in (a, b):
            g.ground(f)
        merged = merge_findings([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(merged[0].votes), ["a", "b"])

    def test_parse_json_variants(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('<think>x</think>Ответ: {"a": [1, 2,],}'), {"a": [1, 2]})


if __name__ == "__main__":
    unittest.main()


class GroundingTest(unittest.TestCase):
    """Цитаты: короткие — только рядом с подсказкой; нечёткие — только без подмены смысла, текст берётся из документа."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.doc = load_document(root / "docs" / "requirements.md")
        cls.g = Grounder(cls.doc, segment(cls.doc))

    def test_short_quote_near_hint(self):
        self.assertEqual(self.g.find("GET", 104)[:3], (104, 104, "exact"))
        self.assertEqual(self.g.find("404", 190)[0], 189)

    def test_short_quote_far_from_hint_or_without_hint(self):
        self.assertIsNone(self.g.find("GET", 10))
        self.assertIsNone(self.g.find("GET", None))

    def test_fuzzy_rejects_changed_meaning(self):
        self.assertIsNone(self.g.find("Месяц определяется по дате проведения покупки в Алматы", 38))
        self.assertIsNone(self.g.find("На карту приходится максимум два таких push за календарный месяц", 39))

    def test_fuzzy_accepts_typo_and_uses_document_text(self):
        hit = self.g.find("Месяц определяется по дате совершения покупок в Алматы", 38)
        self.assertEqual(hit[:3], (38, 38, "fuzzy"))
        self.assertIn("совершения покупки", hit[3])

    def test_ground_replaces_quote_text(self):
        f = Finding(source="llm:x", category="ambiguity", title="t",
                    quotes=[Quote(text="Месяц определяется по дате совершения покупок в Алматы", line=38)])
        self.assertTrue(self.g.ground(f))
        self.assertIn("совершения покупки", f.quotes[0].text)


class RulesNotOverfitTest(unittest.TestCase):
    """Правила не должны ругаться на исправленную версию ТЗ, где дефекты закрыты (таблица кодов, токен карты, ×100)."""

    def test_fixed_spec_has_almost_no_rule_findings(self):
        root = Path(__file__).resolve().parent.parent
        doc = load_document(root / "eval" / "samples" / "requirements_fixed.md")
        ids = sorted({f.source for f in run_rules(doc, segment(doc))[0]})
        for bad in ("rule:status_undescribed", "rule:card_data", "rule:units"):
            self.assertNotIn(bad, ids)
        self.assertLessEqual(len(ids), 2, ids)


class SegmentOtherStructuresTest(unittest.TestCase):
    """Разделы с нестандартными названиями и многоуровневая нумерация не теряются."""

    def _reqs(self, text: str, name: str = "t.md"):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / name
            p.write_text(textwrap.dedent(text), encoding="utf-8")
            doc = load_document(p)
            return segment(doc)

    def test_security_performance_constraints_kept(self):
        reqs = self._reqs("""
            # ТЗ
            ## Функциональные требования
            - Система сохраняет заявку.
            ## Требования к безопасности
            - Пароли хранятся в виде хэша.
            - Сессия истекает через 15 минут.
            ## Требования к производительности
            - Ответ API не дольше 300 мс.
            ## Ограничения
            - Используется только PostgreSQL 15.
            ## Порядок работ
            Исполнитель должен передать исходный код до подписания акта.
            ## Общие сведения
            Заказчик: учебный.
            """)
        texts = " ".join(r.text for r in reqs)
        for must in ("хэша", "15 минут", "300 мс", "PostgreSQL", "исходный код"):
            self.assertIn(must, texts)
        self.assertNotIn("Заказчик", texts)

    def test_multilevel_numbering_is_a_list_item(self):
        reqs = self._reqs("""
            # Техническая спецификация
            ## Требования к услуге
            2.1. Поставщик обеспечивает доступность 99,9%.
            2.2. Поставщик должен вести журнал событий.
            """)
        self.assertEqual([r.doc_ref for r in reqs], ["п. 2.1", "п. 2.2"])
