# Материалы для сдачи

| Файл | Что это |
| :--- | :--- |
| `report.md` | **отчёт агента** о проверке выданного документа `docs/requirements.pdf` (основной файл) |
| `report.pdf` | то же заключение в PDF |
| `report.html` | то же заключение для браузера (кнопки скачивания MD / PDF / HTML) |
| `report.json` | трассировка прогона: все находки, решения критика, модели, статусы требований |
| `report.source.txt` | текст документа с номерами строк, на которые ссылается отчёт |
| `from_md/` | та же проверка по Markdown-версии документа (`docs/requirements.md`) |
| `extra/` | заключения по трём настоящим ТЗ из госзакупок РК (русский DOCX, казахские сканы) |

## Архитектура

![Архитектура ReqAudit](../docs/architecture.png)

<details><summary>Тот же граф в Mermaid</summary>

```mermaid
flowchart TD
    IN([Документ: PDF, скан, DOCX, MD]) --> ING[1 · Текст и номера строк<br/><i>код + OCR</i>]
    ING --> SEG[2 · Нарезка на требования<br/><i>код</i>]
    SEG --> RUL[3 · 15 правил<br/><i>код</i>]
    SEG --> P1[4a · Каждое требование<br/><i>Claude</i>]
    SEG --> P2[4b · Разбор за 5 ролей<br/><i>Claude</i>]
    SEG --> P3[4c · Два разработчика<br/><i>Claude</i>]
    SEG --> AC[4d · Покрытие приёмкой<br/><i>Claude</i>]
    RUL --> GR[5 · Проверка цитат<br/><i>код</i>]
    P1 --> GR
    P2 --> GR
    P3 --> GR
    AC --> VER
    GR --> MRG[6 · Склейка дублей<br/><i>код</i>]
    MRG --> CR[7 · Критик опровергает находки<br/><i>Claude</i>]
    CR --> VER[8 · Вердикт по правилу<br/><i>код</i>]
    VER --> OUT([Заключение: MD, PDF, HTML, JSON])
    CR -. флаг interactive .-> HUM[Человек: до 3 вопросов]
    HUM -.-> VER
    classDef llm fill:#eef1ff,stroke:#3d5afe,color:#1b1b1d
    classDef code fill:#f3f1ec,stroke:#9a978f,color:#1b1b1d
    classDef io fill:#ffffff,stroke:#1b1b1d,color:#1b1b1d
    classDef human fill:#fdf1e2,stroke:#c27412,color:#1b1b1d,stroke-dasharray:4 3
    class P1,P2,P3,AC,CR llm
    class ING,SEG,RUL,GR,MRG,VER code
    class IN,OUT io
    class HUM human
```

</details>

<sub>Синие блоки — Claude Opus 5.5, серые — детерминированный код, пунктир — необязательный шаг (по умолчанию выключен). Подробнее — в [README](../README.md#архитектура).</sub>

Проверяемый документ — [`../docs/requirements.pdf`](../docs/requirements.pdf) (и `.md`), задание — [`../docs/TASK.md`](../docs/TASK.md).

Воспроизвести:

```bash
python -m reqaudit docs/requirements.pdf submission/report.md --no-cache
```
