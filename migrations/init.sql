PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS ohlcv (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  tf_sec INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  PRIMARY KEY (venue, symbol, tf_sec, ts)
);

CREATE TABLE IF NOT EXISTS ticker_snap (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  last REAL,
  bid REAL,
  ask REAL,
  vol24h REAL,
  turnover24h REAL,
  PRIMARY KEY (venue, symbol, ts)
);

CREATE TABLE IF NOT EXISTS sentiment (
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  ts INTEGER NOT NULL,
  sentiment REAL NOT NULL,
  velocity REAL NOT NULL,
  volume INTEGER NOT NULL,
  sources_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  PRIMARY KEY (scope, key, ts)
);

CREATE TABLE IF NOT EXISTS features (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  features_json TEXT NOT NULL,
  PRIMARY KEY (venue, symbol, ts)
);

CREATE TABLE IF NOT EXISTS market_regime (
  ts INTEGER PRIMARY KEY,
  regime_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
  rec_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  bot_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  account_mode TEXT NOT NULL,
  margin_mode TEXT NOT NULL,
  score REAL NOT NULL,
  confidence REAL NOT NULL,
  expected_rr REAL NOT NULL,
  risk_score REAL NOT NULL,
  params_json TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  blocks_json TEXT NOT NULL,
  status TEXT NOT NULL,
  ttl_sec INTEGER NOT NULL,
  model_version TEXT NOT NULL,
  features_ref_ts INTEGER NOT NULL,
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


-- Mutable operator state: one row per strategy family and symbol.
-- Immutable recommendations remains an event/outcome-root audit ledger.
CREATE TABLE IF NOT EXISTS recommendation_latest (
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  bot_type TEXT NOT NULL,
  rec_id TEXT NOT NULL,
  evaluated_ts INTEGER NOT NULL,
  state_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  direction TEXT NOT NULL,
  confidence REAL NOT NULL,
  score REAL NOT NULL,
  candidate_kind TEXT NOT NULL,
  model_version TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (venue, symbol, bot_type)
);
CREATE INDEX IF NOT EXISTS idx_recommendation_latest_status
  ON recommendation_latest(venue, status, confidence DESC, score DESC);
CREATE INDEX IF NOT EXISTS idx_recommendation_latest_rec_id
  ON recommendation_latest(rec_id);

CREATE TABLE IF NOT EXISTS decision_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  action TEXT NOT NULL,
  rec_id TEXT,
  operator TEXT,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_decision_action_ts ON decision_log(action, ts DESC);

CREATE TABLE IF NOT EXISTS bot_instances (
  bot_id TEXT PRIMARY KEY,
  started_ts INTEGER NOT NULL,
  stopped_ts INTEGER,
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
  ts INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  pnl REAL NOT NULL,
  fee REAL NOT NULL,
  funding REAL NOT NULL DEFAULT 0,
  slippage REAL NOT NULL DEFAULT 0,
  meta_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id, ts DESC);

CREATE TABLE IF NOT EXISTS execution_evidence (
  event_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  origin_rec_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  external_event_id TEXT NOT NULL,
  external_order_id TEXT,
  side TEXT,
  qty REAL,
  price REAL,
  order_price REAL,
  benchmark_price REAL,
  benchmark_ts INTEGER,
  benchmark_source TEXT,
  gross_pnl REAL NOT NULL,
  fee REAL NOT NULL,
  funding REAL NOT NULL,
  slippage REAL NOT NULL,
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
  ts INTEGER NOT NULL,
  source TEXT NOT NULL,
  external_snapshot_id TEXT NOT NULL,
  position_qty REAL NOT NULL,
  open_order_count INTEGER NOT NULL,
  execution_event_count INTEGER NOT NULL,
  funding_event_count INTEGER NOT NULL,
  realized_pnl_gross REAL NOT NULL,
  fee REAL NOT NULL,
  funding REAL NOT NULL,
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
  updated_ts INTEGER NOT NULL
);


CREATE TABLE IF NOT EXISTS runtime_locks (
  lock_key TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  heartbeat_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_limits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  limits_json TEXT NOT NULL,
  is_active INTEGER NOT NULL,
  created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reco_outcomes (
  rec_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  bot_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  horizon_sec INTEGER NOT NULL,
  label_available_ts INTEGER,
  entry_close REAL NOT NULL,
  exit_close REAL NOT NULL,
  ret REAL NOT NULL,
  success INTEGER NOT NULL,
  event_type TEXT NOT NULL DEFAULT 'LEGACY_BINARY'
);

CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON reco_outcomes(ts DESC);

CREATE TABLE IF NOT EXISTS reco_outcome_observability (
  rec_id TEXT PRIMARY KEY,
  recommendation_ts INTEGER NOT NULL,
  label_due_ts INTEGER,
  last_attempt_ts INTEGER NOT NULL,
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcome_observability_due
  ON reco_outcome_observability(state, label_due_ts, recommendation_ts DESC);

-- Funding rate snapshots (linear only)
CREATE TABLE IF NOT EXISTS funding_rate (
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  funding_rate REAL NOT NULL,
  next_funding_ts INTEGER,
  funding_interval_min REAL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_ts ON funding_rate(ts DESC);

-- Settled funding rates from Bybit /v5/market/funding/history.
-- Unlike ticker fundingRate forecasts, these rows are immutable historical
-- settlements and are the only funding source allowed in proxy outcome labels.
CREATE TABLE IF NOT EXISTS funding_settlement (
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  funding_rate REAL NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_settlement_ts ON funding_settlement(symbol, ts);

-- Durable targeted recovery queue for missing historical funding settlements.
CREATE TABLE IF NOT EXISTS funding_settlement_repair (
  repair_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  expected_ts INTEGER,
  range_start_ts INTEGER NOT NULL,
  range_end_ts INTEGER NOT NULL,
  first_requested_ts INTEGER NOT NULL,
  last_attempt_ts INTEGER,
  next_attempt_ts INTEGER NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  last_error TEXT,
  updated_ts INTEGER NOT NULL
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
  trade_ts_ms INTEGER NOT NULL,
  seq INTEGER,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  qty REAL NOT NULL,
  received_ts_ms INTEGER NOT NULL,
  source TEXT NOT NULL,
  is_block_trade INTEGER NOT NULL DEFAULT 0,
  is_rpi_trade INTEGER NOT NULL DEFAULT 0,
  stream_session_id TEXT,
  stream_message_index BIGINT,
  stream_row_index INTEGER,
  stream_message_ts_ms BIGINT,
  PRIMARY KEY (venue, symbol, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_market_trade_path
  ON market_trade(venue, symbol, trade_ts_ms, seq, trade_id);
CREATE INDEX IF NOT EXISTS idx_market_trade_received
  ON market_trade(received_ts_ms);
CREATE INDEX IF NOT EXISTS idx_market_trade_stream_order
  ON market_trade(venue, symbol, stream_session_id, stream_message_index, stream_row_index);

CREATE TABLE IF NOT EXISTS market_trade_coverage (
  coverage_id TEXT PRIMARY KEY,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  coverage_start_ms INTEGER NOT NULL,
  coverage_end_ms INTEGER NOT NULL,
  state TEXT NOT NULL,
  source TEXT NOT NULL,
  last_poll_ts_ms INTEGER,
  gap_reason TEXT,
  details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_trade_coverage_window
  ON market_trade_coverage(venue, symbol, coverage_start_ms, coverage_end_ms);
CREATE INDEX IF NOT EXISTS idx_market_trade_coverage_state
  ON market_trade_coverage(state, venue, symbol, coverage_end_ms);

-- Open interest snapshots (linear only)
CREATE TABLE IF NOT EXISTS open_interest (
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  oi REAL NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_oi_ts ON open_interest(ts DESC);
