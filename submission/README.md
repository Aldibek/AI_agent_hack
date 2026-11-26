# Материалы для сдачи

| Файл | Что это |
| :--- | :--- |
| `TASK.md` | Задание хакатона BCC-HUB x MNU ([источник](https://github.com/BCC-HUB/ai-requirements-validator-task)) |
| `requirements.md` | Проверяемый документ, выданный организаторами |
| `report.md` | Отчёт агента о проверке (основной файл) |
| `report.html` | Тот же отчёт в наглядном виде: подсветка строк документа, фильтры |
| `report.json` | Трассировка прогона: все находки, решения судьи, модели |
| `report.source.txt` | Текст документа с номерами строк, на которые ссылается отчёт |

Как воспроизвести:

```bash
python -m reqaudit submission/requirements.md submission/report.md --no-cache
```
