# Модель торговли: быстрый контекст

Документ для того, чтобы за 5 минут понять, как устроена «модель» бота, где что лежит и как проверять. Полный список функций: [MODEL_FUNCTIONS.md](MODEL_FUNCTIONS.md) (генерируется `python scripts/list_functions.py > docs/MODEL_FUNCTIONS.md`). Результаты аудита: [MODEL_AUDIT.md](MODEL_AUDIT.md).

## Из чего состоит

| Слой | Файлы | Что делает |
|---|---|---|
| Сигнал стратегии | `app/services/strategy_engine.py` | По свечам 1 мин даёт BUY/SELL/HOLD и score. Режимы: `trend`, `mean_reversion`, `breakout` (`evaluate_signal`). Это единственный «классический» источник сигналов. |
| Признаки | `app/ml/feature_builder.py`, `multi_timeframe.py` | `build_features` собирает 30 признаков (`FEATURE_NAMES`): z-score, SMA-gap, momentum, пробой, объём, ATR, тренд/волатильность 1ч и 4ч, стакан, RSI/MACD/BB, время суток, VWAP, фаза сессии, режим рынка, корреляция с SBER/GAZP, код тикера, риск переноса шорта. |
| ML-модель | `app/ml/model_trainer.py`, `model_predictor.py` | `GradientBoostingClassifier` + калибровка Platt, предсказывает P(сделка прибыльная). Модели хранятся в таблице `ml_models` (pickle), одна активная на бумагу + одна универсальная на все бумаги. |
| Решения по модели | `model_predictor.py` | Фильтр входа `should_enter` (порог 0.60, жёсткое вето только после 100 сделок, ниже — мета-правило «2 из 3»: score, тренд 1ч, P), досрочный выход `should_exit_early` (P < 0.30, держать не менее 20 мин), масштаб лота `compute_lot_scale`, адаптивные SL/TP `compute_adaptive_sl_tp`, ранжирование бумаг `rank_instruments`. |
| Обучение на опыте | `app/ml/experience.py`, `analyzer.py`, `optimizer.py`, `strategy_selector.py`, `orchestrator.py` | Контекст каждой сделки (`ml_trade_context`), качество стратегии (expectancy, EWA), подбор SL/TP/min_score координатным спуском, выбор режима стратегии Thompson Sampling, суточная перебалансировка и детектор дрейфа. |
| Бэктест и аналитик | `app/services/backtest_engine.py`, `analyst.py`, `app/ml/backtest_client.py` | Прогон стратегий на истории, поиск лучших параметров, сохранение результата как стратегии. |

## Жизненный цикл решения (main.py)

1. Воркер стратегии берёт свечи, `_evaluate_signal(tradingmode, candles)` → сигнал и score.
2. Фильтры без ML: сессия, лимиты (сделок/убытка за день), cooldown, баланс, встречная позиция по бумаге (защита от неттинга, v5.8.9).
3. `build_features` → `should_enter` (ML-вето/мета-правило). Если вход разрешён: `compute_adaptive_sl_tp`, `compute_lot_scale`.
4. Ордер → нативные SL/TP на бирже → `record_entry` (контекст) и `store_features` (признаки без метки).
5. Пока позиция открыта: программные SL/TP в цикле, `should_exit_early` по ML.
6. Закрытие (бот, биржевой стоп или вручную): `add_trade` → `label_features` (метка по PnL) → `on_trade_closed` (быстрое EWA-обновление) → `record_exit` → при накоплении 10 новых сделок `train_model`.
7. Раз в сутки (00:00 МСК) `run_daily_rebalance`: обучение универсальной модели, глубокий анализ по бумагам, ранжирование, детектор дрейфа. Он **меняет режим и SL/TP боевых стратегий**, запускать вручную осторожно.

## Таблицы БД

`ml_features` (признаки и метка), `ml_models` (pickle-модели и метрики), `ml_decisions` (журнал решений), `ml_trade_context` (контекст сделок), `ml_instrument_state` (EWA-качество и выученные параметры по бумаге), `ml_optimization_log` (история оптимизаций). Связь с торговлей: `trades`, `positions`, `strategy_instruments`, `strategy_settings`.

## Ключевые пороги

| Константа | Значение | Смысл |
|---|---|---|
| `ENTRY_THRESHOLD` | 0.60 | P выше — вход |
| `HARD_VETO_MIN_SAMPLES` | 100 | с этого числа сделок порог становится жёстким |
| `EXIT_THRESHOLD` / `MIN_HOLD_MINUTES` | 0.30 / 20 | досрочный выход |
| `MIN_SAMPLES` / `RETRAIN_EVERY` / `TEST_RATIO` | 30 / 10 / 0.2 | обучение: минимум, частота, доля теста (последние по времени) |
| `MIN_PRECISION_ACTIVATE` | 0.50 | минимальная precision для активации универсальной модели |
| `MIN_STOP_LOSS_PCT` (main.py) | 0.5% | пол стопа |
| `EWA_ALPHA` / `MIN_TRADES` | 0.2 / 5 | сглаживание качества |

## Как проверить модель

- Данные и логи сервера: MCP `trading-bot-server` (локально, в git не хранится): `trade_stats`, `run_select`, `get_events`, `error_summary`.
- Тест на копии БД (не на продакшне): снимок `sqlite3 data/trading_bot.sqlite3 ".backup /tmp/x.sqlite3"`, запуск с `DB_PATH=/tmp/x.sqlite3`.
- Что нельзя запускать на продакшне ради проверки: `run_daily_rebalance`, `_apply_*` в `orchestrator.py` (меняют боевые настройки).

## Известные ограничения

- Реальных размеченных сделок мало (десятки), поэтому жёсткое ML-вето пока выключено и работает мета-правило «2 из 3».
- Брокер ведёт позицию нетто по бумаге, модель и учёт — по стратегиям; расхождения ловит `POSITION_MISMATCH`.
- Sandbox-цены и ликвидность не равны реальным, метрики модели нельзя переносить на боевой счёт без повторной проверки.
