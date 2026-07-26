CREATE TABLE IF NOT EXISTS ohlcv (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  tf_sec INTEGER NOT NULL,
  ts BIGINT NOT NULL,
  open DOUBLE PRECISION NOT NULL,
  high DOUBLE PRECISION NOT NULL,
  low DOUBLE PRECISION NOT NULL,
  close DOUBLE PRECISION NOT NULL,
  volume DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (venue, symbol, tf_sec, ts)
);

CREATE TABLE IF NOT EXISTS ticker_snap (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  last DOUBLE PRECISION,
  bid DOUBLE PRECISION,
  ask DOUBLE PRECISION,
  vol24h DOUBLE PRECISION,
  turnover24h DOUBLE PRECISION,
  PRIMARY KEY (venue, symbol, ts)
);

CREATE TABLE IF NOT EXISTS sentiment (
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  ts BIGINT NOT NULL,
  sentiment DOUBLE PRECISION NOT NULL,
  velocity DOUBLE PRECISION NOT NULL,
  volume INTEGER NOT NULL,
  sources_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  PRIMARY KEY (scope, key, ts)
);

CREATE TABLE IF NOT EXISTS features (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  features_json TEXT NOT NULL,
  PRIMARY KEY (venue, symbol, ts)
);

CREATE TABLE IF NOT EXISTS market_regime (
  ts BIGINT PRIMARY KEY,
  regime_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
  rec_id TEXT PRIMARY KEY,
  ts BIGINT NOT NULL,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  bot_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  account_mode TEXT NOT NULL,
  margin_mode TEXT NOT NULL,
  score DOUBLE PRECISION NOT NULL,
  confidence DOUBLE PRECISION NOT NULL,
  expected_rr DOUBLE PRECISION NOT NULL,
  risk_score DOUBLE PRECISION NOT NULL,
  params_json TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  blocks_json TEXT NOT NULL,
  status TEXT NOT NULL,
  ttl_sec INTEGER NOT NULL,
  model_version TEXT NOT NULL,
  features_ref_ts BIGINT NOT NULL,
  publication_root_rec_id TEXT,
  outcome_root_rec_id TEXT,
  is_outcome_label_root INTEGER NOT NULL DEFAULT 1,
  outcome_eligible INTEGER,
  policy_evaluation_eligible INTEGER,
  outcome_sample_role TEXT,
  risk_checks_passed INTEGER,
  risk_blocks_empty INTEGER,
  llm_review_status TEXT,
  candidate_kind TEXT NOT NULL DEFAULT 'strategy_recommendation'
);

CREATE INDEX IF NOT EXISTS idx_reco_ts ON recommendations(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_symbol ON recommendations(venue, symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_status_ts ON recommendations(status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_venue_status_ts ON recommendations(venue, status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_publication_root_ts ON recommendations(publication_root_rec_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_outcome_lineage_ts ON recommendations(outcome_root_rec_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_outcome_root_ts ON recommendations(is_outcome_label_root, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_model_outcome_scope ON recommendations(model_version, is_outcome_label_root, rec_id);
CREATE INDEX IF NOT EXISTS idx_reco_candidate_kind_ts ON recommendations(candidate_kind, bot_type, ts DESC);

CREATE TABLE IF NOT EXISTS decision_log (
  id BIGSERIAL PRIMARY KEY,
  ts BIGINT NOT NULL,
  action TEXT NOT NULL,
  rec_id TEXT,
  operator TEXT,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_decision_action_ts ON decision_log(action, ts DESC);

CREATE TABLE IF NOT EXISTS bot_instances (
  bot_id TEXT PRIMARY KEY,
  started_ts BIGINT NOT NULL,
  stopped_ts BIGINT,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  bot_type TEXT NOT NULL,
  mode_json TEXT NOT NULL,
  params_json TEXT NOT NULL,
  state_json TEXT NOT NULL,
  status TEXT NOT NULL,
  origin_rec_id TEXT,
  publication_root_rec_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_bots_status ON bot_instances(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_origin_rec_unique ON bot_instances(origin_rec_id) WHERE origin_rec_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bot_publication_root_status ON bot_instances(publication_root_rec_id, status, started_ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_running_publication_root_unique ON bot_instances(publication_root_rec_id) WHERE publication_root_rec_id IS NOT NULL AND status='running';

CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  ts BIGINT NOT NULL,
  symbol TEXT NOT NULL,
  pnl DOUBLE PRECISION NOT NULL,
  fee DOUBLE PRECISION NOT NULL,
  funding DOUBLE PRECISION NOT NULL DEFAULT 0,
  slippage DOUBLE PRECISION NOT NULL DEFAULT 0,
  meta_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id, ts DESC);

CREATE TABLE IF NOT EXISTS execution_evidence (
  event_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  origin_rec_id TEXT NOT NULL,
  ts BIGINT NOT NULL,
  symbol TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  external_event_id TEXT NOT NULL,
  external_order_id TEXT,
  side TEXT,
  qty DOUBLE PRECISION,
  price DOUBLE PRECISION,
  order_price DOUBLE PRECISION,
  benchmark_price DOUBLE PRECISION,
  benchmark_ts BIGINT,
  benchmark_source TEXT,
  gross_pnl DOUBLE PRECISION NOT NULL,
  fee DOUBLE PRECISION NOT NULL,
  funding DOUBLE PRECISION NOT NULL,
  slippage DOUBLE PRECISION NOT NULL,
  currency TEXT NOT NULL,
  meta_json TEXT NOT NULL,
  FOREIGN KEY (bot_id) REFERENCES bot_instances(bot_id),
  FOREIGN KEY (origin_rec_id) REFERENCES recommendations(rec_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_evidence_external_unique
  ON execution_evidence(source, external_event_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_bot_ts
  ON execution_evidence(bot_id, ts ASC, event_id ASC);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_rec_ts
  ON execution_evidence(origin_rec_id, ts ASC, event_id ASC);

CREATE TABLE IF NOT EXISTS execution_reconciliations (
  reconciliation_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  origin_rec_id TEXT NOT NULL,
  ts BIGINT NOT NULL,
  source TEXT NOT NULL,
  external_snapshot_id TEXT NOT NULL,
  position_qty DOUBLE PRECISION NOT NULL,
  open_order_count BIGINT NOT NULL,
  execution_event_count BIGINT NOT NULL,
  funding_event_count BIGINT NOT NULL,
  realized_pnl_gross DOUBLE PRECISION NOT NULL,
  fee DOUBLE PRECISION NOT NULL,
  funding DOUBLE PRECISION NOT NULL,
  currency TEXT NOT NULL,
  complete INTEGER NOT NULL,
  meta_json TEXT NOT NULL,
  FOREIGN KEY (bot_id) REFERENCES bot_instances(bot_id),
  FOREIGN KEY (origin_rec_id) REFERENCES recommendations(rec_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_reconciliations_external_unique
  ON execution_reconciliations(source, external_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_execution_reconciliations_bot_ts
  ON execution_reconciliations(bot_id, ts DESC, reconciliation_id DESC);

CREATE TABLE IF NOT EXISTS app_config (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_ts BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_locks (
  lock_key TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  heartbeat_ts BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_limits (
  id BIGSERIAL PRIMARY KEY,
  version TEXT NOT NULL,
  limits_json TEXT NOT NULL,
  is_active INTEGER NOT NULL,
  created_ts BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS reco_outcomes (
  rec_id TEXT PRIMARY KEY,
  ts BIGINT NOT NULL,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  bot_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  horizon_sec INTEGER NOT NULL,
  label_available_ts BIGINT,
  entry_close DOUBLE PRECISION NOT NULL,
  exit_close DOUBLE PRECISION NOT NULL,
  ret DOUBLE PRECISION NOT NULL,
  success INTEGER NOT NULL,
  event_type TEXT NOT NULL DEFAULT 'LEGACY_BINARY'
);

CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON reco_outcomes(ts DESC);

CREATE TABLE IF NOT EXISTS reco_outcome_observability (
  rec_id TEXT PRIMARY KEY,
  recommendation_ts BIGINT NOT NULL,
  label_due_ts BIGINT,
  last_attempt_ts BIGINT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcome_observability_due
  ON reco_outcome_observability(state, label_due_ts, recommendation_ts DESC);

CREATE TABLE IF NOT EXISTS funding_rate (
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  funding_rate DOUBLE PRECISION NOT NULL,
  next_funding_ts BIGINT,
  funding_interval_min DOUBLE PRECISION,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_ts ON funding_rate(ts DESC);

-- Settled funding rates from Bybit /v5/market/funding/history.
-- Unlike ticker fundingRate forecasts, these rows are immutable historical
-- settlements and are the only funding source allowed in proxy outcome labels.
CREATE TABLE IF NOT EXISTS funding_settlement (
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  funding_rate DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_settlement_ts ON funding_settlement(symbol, ts);

-- Durable targeted recovery queue for missing historical funding settlements.
CREATE TABLE IF NOT EXISTS funding_settlement_repair (
  repair_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  expected_ts BIGINT,
  range_start_ts BIGINT NOT NULL,
  range_end_ts BIGINT NOT NULL,
  first_requested_ts BIGINT NOT NULL,
  last_attempt_ts BIGINT,
  next_attempt_ts BIGINT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  last_error TEXT,
  updated_ts BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funding_repair_due
  ON funding_settlement_repair(state, next_attempt_ts, updated_ts);
CREATE INDEX IF NOT EXISTS idx_funding_repair_symbol
  ON funding_settlement_repair(symbol, expected_ts);

-- Public trade journal used only to resolve intrabar price chronology. It does
-- not claim queue priority or exact live fills.
CREATE TABLE IF NOT EXISTS market_trade (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  trade_id TEXT NOT NULL,
  trade_ts_ms BIGINT NOT NULL,
  seq BIGINT,
  side TEXT NOT NULL,
  price DOUBLE PRECISION NOT NULL,
  qty DOUBLE PRECISION NOT NULL,
  received_ts_ms BIGINT NOT NULL,
  source TEXT NOT NULL,
  is_block_trade INTEGER NOT NULL DEFAULT 0,
  is_rpi_trade INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (venue, symbol, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_market_trade_path
  ON market_trade(venue, symbol, trade_ts_ms, seq, trade_id);
CREATE INDEX IF NOT EXISTS idx_market_trade_received
  ON market_trade(received_ts_ms);

CREATE TABLE IF NOT EXISTS market_trade_coverage (
  coverage_id TEXT PRIMARY KEY,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  coverage_start_ms BIGINT NOT NULL,
  coverage_end_ms BIGINT NOT NULL,
  state TEXT NOT NULL,
  source TEXT NOT NULL,
  last_poll_ts_ms BIGINT,
  gap_reason TEXT,
  details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_trade_coverage_window
  ON market_trade_coverage(venue, symbol, coverage_start_ms, coverage_end_ms);
CREATE INDEX IF NOT EXISTS idx_market_trade_coverage_state
  ON market_trade_coverage(state, venue, symbol, coverage_end_ms);

CREATE TABLE IF NOT EXISTS open_interest (
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  oi DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_oi_ts ON open_interest(ts DESC);
