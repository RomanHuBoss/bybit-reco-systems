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
  features_ref_ts INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reco_ts ON recommendations(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reco_symbol ON recommendations(venue, symbol, ts DESC);

CREATE TABLE IF NOT EXISTS decision_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  action TEXT NOT NULL,
  rec_id TEXT,
  operator TEXT,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_log(ts DESC);

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
  origin_rec_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_bots_status ON bot_instances(status);

CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  pnl REAL NOT NULL,
  fee REAL NOT NULL,
  meta_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);

CREATE TABLE IF NOT EXISTS app_config (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_limits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  limits_json TEXT NOT NULL,
  is_active INTEGER NOT NULL,
  created_ts INTEGER NOT NULL
);

