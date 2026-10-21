# Инвентарь функций модели торговли

Файл сгенерирован `scripts/list_functions.py`; колонка «Статус теста» заполняется при аудите.

## app/ml/orchestrator.py (14)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 22 | `on_trade_closed` | figi, pnl | Вызывается после закрытия позиции. | 8 |
| 32 | `run_daily_rebalance` |  | Глубокий анализ — запускается при смене торгового дня (00:00 МСК) из | 39 |
| 73 | `_rank_instruments_daily` | instruments | Фаза 5: ранжирует инструменты по ML, временно отключает слабые. | 31 |
| 106 | `get_ml_params` | figi, strategy_id | Возвращает выученные параметры для инструмента если confidence достаточная. | 20 |
| 128 | `get_learning_summary` |  | Сводка по состоянию обучения (для дашборда). | 20 |
| 152 | `_soft_update` | figi, pnl | Быстрое обновление EWA quality_score после каждой сделки. | 21 |
| 175 | `_deep_update` | figi, ticker, strategy_id, current_params | Полный анализ и возможное обновление параметров. | 82 |
| 259 | `_apply_strategy_mode` | figi, strategy_id, mode | Обновляет tradingmode в strategy_settings. | 12 |
| 273 | `_apply_param` | figi, strategy_id, param, value | Обновляет SL/TP в strategy_instruments. | 11 |
| 286 | `_apply_strategy_score` | strategy_id, score | Обновляет min_signal_score в strategy_settings. | 12 |
| 300 | `_get_active_instruments` |  | Возвращает все активные параллельные инструменты с их параметрами. | 36 |
| 338 | `_get_strategy_id` | figi | Находит strategy_id для figi из активного профиля. | 18 |
| 358 | `_check_concept_drift` |  | Мониторинг просадки качества: считает WIN-RATE последних 20 размеченных | 39 |
| 399 | `_get_last_rebalance` |  | Время последней оптимизации. | 10 |

## app/ml/feature_builder.py (10)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 12 | `_sma` | values, n |  | 3 |
| 17 | `_stddev` | values, n |  | 6 |
| 25 | `build_features` | figi, ticker, candles_5m, signal_score, signal_action, indicators, orderbook, candles_1h, candles_4h, position_minutes | Собирает 30 признаков. Все числовые, NaN заменяются на 0. | 252 |
| 129 | `_num` | key, default |  | 3 |
| 300 | `get_ticker_code` | ticker | Стабильный числовой код тикера для универсальной модели (CRC32 — не зависит от PYTHONHASHSEED). | 6 |
| 308 | `features_to_vector` | features | Конвертирует dict признаков в вектор для sklearn. | 3 |
| 313 | `store_features` | figi, ticker, strategy_id, features, trade_id | Сохраняет признаки в ml_features. Возвращает id записи. | 21 |
| 336 | `label_features` | feature_id, pnl, quality_score | Размечает результат сделки. Градуированные метки: | 22 |
| 365 | `update_sector_returns` | figi, returns | Обновляет кеш доходностей для рыночных маркеров. | 4 |
| 371 | `compute_sector_correlation` | figi, own_returns | Корреляция инструмента с рыночными маркерами (SBER, GAZP). | 27 |

## app/ml/multi_timeframe.py (6)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 22 | `_aggregate` | candles, k | Склеивает каждые k подряд идущих свечей в одну (считая с последней), порядок — по времени. | 17 |
| 41 | `get_candles_cached` | client, figi, interval, n | Свечи 1ч или 4ч с кешированием по TTL таймфрейма. | 26 |
| 69 | `get_all_timeframes` | client, figi | Загружает 1ч/4ч свечи для одного инструмента. | 6 |
| 77 | `trend_direction` | candles, fast, slow | Направление тренда: +1 бычий, -1 медвежий, 0 нейтральный. | 14 |
| 93 | `compute_volatility` | candles, n | Реализованная волатильность (stddev returns × sqrt(252×n_bars_per_day)). | 11 |
| 106 | `compute_atr` | candles, n | ATR как % от цены. | 16 |

## app/ml/model_trainer.py (11)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 22 | `_balance_classes` | X_train, y_train | Oversample миноритарного класса до уровня мажоритарного (детерминированно). | 22 |
| 46 | `_archive_active_models` | cur, figi, strategy_id | Помечает прежние active-модели как archived — чтобы оставалась одна активная на figi. | 9 |
| 57 | `get_all_labeled_features` |  | Загружает ВСЕ размеченные признаки со всех инструментов (для универсальной модели). | 19 |
| 78 | `train_universal_model` |  | Универсальная модель на ВСЕХ инструментах. | 87 |
| 167 | `_notify_universal_ready` | n, prec, metrics |  | 14 |
| 183 | `get_training_data` | figi, strategy_id | Загружает размеченные признаки из ml_features. | 19 |
| 204 | `train_model` | figi, ticker, strategy_id | Обучает GradientBoostingClassifier + Platt calibration на накопленных данных. | 98 |
| 304 | `_notify_model_ready` | ticker, n_samples, precision, metrics | Отправляет Telegram когда модель достигла порога точности. | 16 |
| 322 | `is_model_active` | figi, strategy_id | True если модель обучена и активна. | 11 |
| 335 | `load_model` | figi, strategy_id | Загружает последнюю активную модель из БД. | 25 |
| 362 | `should_retrain` | figi, strategy_id | True если с последнего обучения накопилось RETRAIN_EVERY новых сделок. | 19 |

## app/ml/model_predictor.py (17)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 32 | `_universal_model_precision` |  | Precision активной универсальной модели (кеш 5 мин). | 20 |
| 54 | `is_active` | figi, strategy_id | True если модель обучена и готова к автономным решениям. | 4 |
| 60 | `log_decision` | figi, ticker, decision_type, confidence, threshold, executed, reason, features | Сохраняет каждое решение модели в ml_decisions. | 19 |
| 81 | `_get_model` | figi, strategy_id | Загружает модель из кеша или БД. | 11 |
| 94 | `invalidate_cache` | figi, strategy_id | Сбрасывает кеш после переобучения. | 3 |
| 99 | `_get_universal_model` |  | Загружает универсальную модель (обученную на всех инструментах). | 18 |
| 119 | `predict_probability` | figi, strategy_id, features | Возвращает P(прибыльная сделка). | 25 |
| 146 | `compute_lot_scale` | confidence | Масштаб позиции на основе уверенности модели. | 17 |
| 165 | `meta_strategy_check` | features, signal_score, signal_action, min_score | Meta-стратегия '2 из 3': | 23 |
| 193 | `_model_samples` | figi, strategy_id | Сколько сделок было в обучении активной модели (по бумаге, иначе универсальной). Кеш 5 минут. | 25 |
| 220 | `should_enter` | figi, ticker, strategy_id, features, signal_action, signal_score, min_score | Фильтр входа. | 57 |
| 279 | `should_exit_early` | figi, ticker, strategy_id, features, direction, entry_price, current_price, position_minutes | Досрочное закрытие при развороте. | 48 |
| 329 | `compute_adaptive_sl_tp` | features, base_sl, base_tp | Адаптирует SL/TP к текущей волатильности. | 27 |
| 358 | `rank_instruments` | instruments, strategy_id | Ранжирует инструменты по исторической прибыльности (EWA quality_score из | 24 |
| 386 | `_send_ml_decision_tg` | ticker, direction, decision, confidence, details | Отправляет аналитику решения в Telegram. | 16 |
| 404 | `_format_entry_reason` | features, p, icon | Форматирует причины решения входа для Telegram. | 27 |
| 433 | `_format_exit_reason` | features, p, pnl_pct | Форматирует причины досрочного закрытия. | 20 |

## app/ml/experience.py (4)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 13 | `_now_msk` |  |  | 2 |
| 17 | `record_entry` | figi, ticker, strategy_id, strategy_mode, signal_score, indicators, stop_loss_pct, take_profit_pct | Записывает контекст при открытии позиции. | 41 |
| 60 | `record_exit` | figi, pnl, entry_time_str | При закрытии позиции: обновляет последний незакрытый контекст по figi, | 37 |
| 99 | `get_recent_contexts` | figi, days | Возвращает контексты завершённых сделок по инструменту. | 16 |

## app/ml/strategy_selector.py (3)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 15 | `_sample_beta` | wins, losses | Сэмплирует из Beta(wins+1, losses+1). | 8 |
| 25 | `select_best_strategy_mode` | figi, current_mode | Выбирает лучший режим стратегии через Thompson Sampling. | 37 |
| 64 | `get_mode_stats` | figi | Возвращает статистику по всем режимам для инструмента (для дашборда). | 31 |

## app/ml/optimizer.py (6)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 21 | `_is_improvement` | q, base | Знак-безопасная проверка улучшения. Прежняя q > base*(1+thr) ломалась при | 8 |
| 31 | `_compute_expectancy` | contexts | Вычисляет Expectancy (quality score) по списку сделок. | 10 |
| 43 | `_filter_by_params` | contexts, sl, tp, score | Симулирует отбор сделок по новым параметрам: | 17 |
| 62 | `suggest_sl_tp` | figi, strategy_id, current_sl, current_tp, current_score | Coordinate Descent: пробует ±шаг по каждому параметру, | 53 |
| 117 | `log_optimization` | figi, ticker, strategy_id, param, before, after, reason, q_before, q_after, applied | Записывает факт оптимизации в ml_optimization_log. | 22 |
| 141 | `get_optimization_log` | limit | История оптимизаций для дашборда. | 13 |

## app/ml/analyzer.py (5)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 17 | `compute_quality` | contexts | Вычисляет качество стратегии по списку контекстов (завершённых сделок). | 27 |
| 46 | `update_ewa_quality` | figi, strategy_id, new_quality | Обновляет EWA quality_score в ml_instrument_state после новой сделки. | 29 |
| 77 | `get_instrument_state` | figi, strategy_id | Читает текущее состояние ML-модели для инструмента. | 15 |
| 94 | `upsert_instrument_state` | figi, ticker, strategy_id, stats, ml_params | Создаёт или обновляет состояние ML-модели для инструмента. | 44 |
| 140 | `get_all_states` |  | Все состояния ML-модели для дашборда. | 11 |

## app/ml/backtest_client.py (3)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 21 | `_quality` | res | Метрика качества бэктеста: чистый PnL с поправкой на число сделок. | 12 |
| 35 | `_run` | candles, mode, sl, tp, min_score |  | 7 |
| 44 | `optimize_instrument` | figi, ticker, current_mode, current_sl, current_tp | Полный перебор mode×SL×TP на исторических свечах (свечи грузятся один раз). | 68 |

## app/services/strategy_engine.py (7)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 5 | `sma` | values, n |  | 7 |
| 14 | `stddev` | values, n |  | 7 |
| 23 | `calc_score` | parts |  | 5 |
| 30 | `trend_signal` | candles |  | 14 |
| 46 | `mean_reversion_signal` | candles |  | 14 |
| 62 | `breakout_signal` | candles |  | 19 |
| 83 | `evaluate_signal` | mode, candles |  | 11 |

## app/services/backtest_engine.py (2)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 42 | `run_backtest` | candles, mode, stop_loss_pct, take_profit_pct, commission_pct, initial_capital, qty, min_signal_score | Событийно-управляемый бэктест на OHLCV свечах с сигналами strategy_engine. | 185 |
| 229 | `result_to_dict` | res, candles | Сериализует BacktestResult в JSON-безопасный словарь. | 42 |

## app/services/analyst.py (14)

| Строка | Функция | Аргументы | Назначение | Длина |
|---|---|---|---|---|
| 85 | `_find_optimal_score` | candles, mode, sl, tp, lots, budget_rub | Run backtests at different score thresholds, pick the one that maximizes net_pnl × win_rate × (1-drawdown). | 25 |
| 114 | `get_state` |  |  | 3 |
| 119 | `start` | budget_rub, min_win_rate, min_trades, days, interval, min_pnl, exclude_active, _run_id_override |  | 25 |
| 146 | `stop` |  |  | 3 |
| 153 | `get_opt_state` |  |  | 3 |
| 158 | `start_optimize` | budget_rub, days, interval |  | 14 |
| 174 | `stop_optimize` |  |  | 3 |
| 181 | `_upd` |  |  | 3 |
| 186 | `_worker` | budget_rub, min_win_rate, min_trades, days, interval, min_pnl, exclude_active, _run_id_override |  | 159 |
| 349 | `_opt_upd` |  |  | 3 |
| 354 | `_optimization_worker` | budget_rub, days, interval |  | 184 |
| 542 | `get_results` | limit |  | 11 |
| 555 | `get_result_by_id` | result_id |  | 6 |
| 563 | `save_as_strategy` | result_id, strategy_name | Создаёт новую стратегию из результата аналитика. Возвращает strategy_id. | 87 |

Всего функций: 102
