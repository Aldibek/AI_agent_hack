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
