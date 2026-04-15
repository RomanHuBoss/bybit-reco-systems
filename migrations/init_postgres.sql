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
  is_outcome_label_root INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_reco_ts ON recommendations(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_symbol ON recommendations(venue, symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_status_ts ON recommendations(status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_venue_status_ts ON recommendations(venue, status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_publication_root_ts ON recommendations(publication_root_rec_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_outcome_root_ts ON recommendations(is_outcome_label_root, ts DESC);

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

CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  ts BIGINT NOT NULL,
  symbol TEXT NOT NULL,
  pnl DOUBLE PRECISION NOT NULL,
  fee DOUBLE PRECISION NOT NULL,
  meta_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id, ts DESC);

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
  entry_close DOUBLE PRECISION NOT NULL,
  exit_close DOUBLE PRECISION NOT NULL,
  ret DOUBLE PRECISION NOT NULL,
  success INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON reco_outcomes(ts DESC);

CREATE TABLE IF NOT EXISTS funding_rate (
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  funding_rate DOUBLE PRECISION NOT NULL,
  next_funding_ts BIGINT,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_ts ON funding_rate(ts DESC);

CREATE TABLE IF NOT EXISTS open_interest (
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  oi DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_oi_ts ON open_interest(ts DESC);
