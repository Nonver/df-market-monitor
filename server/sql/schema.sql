-- 三角洲子弹价格监控平台 · 数据库结构
-- MySQL 8.0

SET NAMES utf8mb4;

-- 物品字典（后台可维护）
CREATE TABLE IF NOT EXISTS items (
  propid        VARCHAR(32) PRIMARY KEY COMMENT '官方11位物品ID',
  name          VARCHAR(128) NOT NULL COMMENT '物品全名',
  caliber       VARCHAR(32)  COMMENT '口径',
  grade         VARCHAR(32)  COMMENT '弹种等级',
  second_class  VARCHAR(32)  COMMENT '二级分类',
  watch_enabled TINYINT DEFAULT 1 COMMENT '是否纳入监控',
  sort_order    INT DEFAULT 0,
  created_at    DATETIME,
  updated_at    DATETIME,
  INDEX idx_enabled (watch_enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='物品字典';

-- 小时价格（核心时序表）
CREATE TABLE IF NOT EXISTS price_hourly (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  propid        VARCHAR(32) NOT NULL,
  stat_hour     VARCHAR(10) NOT NULL COMMENT 'YYYYMMDDHH',
  avg_buyprice  DOUBLE COMMENT '官方小时成交均价',
  source        VARCHAR(16) DEFAULT 'official',
  collected_at  DATETIME,
  UNIQUE KEY uk_item_hour (propid, stat_hour),
  INDEX idx_hour (stat_hour),
  INDEX idx_item (propid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='小时价格';

-- 凭证池
CREATE TABLE IF NOT EXISTS credential (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  name            VARCHAR(64) COMMENT '备注名',
  openid          VARCHAR(128),
  access_token    TEXT COMMENT 'AES加密存储',
  acctype         VARCHAR(8) DEFAULT 'qc' COMMENT 'qc=QQ / wx=微信',
  appid           VARCHAR(32) DEFAULT '101491592',
  status          VARCHAR(16) DEFAULT 'active',
  success_count   INT DEFAULT 0,
  fail_count      INT DEFAULT 0,
  last_success_at DATETIME,
  last_fail_at    DATETIME,
  last_error      VARCHAR(255),
  daily_quota     INT DEFAULT 500,
  used_today      INT DEFAULT 0,
  quota_date      DATE,
  remark          VARCHAR(255),
  created_at      DATETIME,
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='凭证池';

-- 采集日志
CREATE TABLE IF NOT EXISTS collect_log (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  started_at    DATETIME,
  finished_at   DATETIME,
  item_count    INT DEFAULT 0,
  success_count INT DEFAULT 0,
  cred_id       INT,
  status        VARCHAR(16),
  error_msg     VARCHAR(500),
  INDEX idx_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='采集日志';

-- 用户
CREATE TABLE IF NOT EXISTS user (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  openid        VARCHAR(64) UNIQUE,
  nickname      VARCHAR(64),
  avatar        VARCHAR(255),
  created_at    DATETIME,
  last_login_at DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户';

-- 自选关注
CREATE TABLE IF NOT EXISTS user_watch (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  user_id    INT,
  propid     VARCHAR(32),
  created_at DATETIME,
  UNIQUE KEY uk_user_item (user_id, propid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自选关注';

-- 持仓
CREATE TABLE IF NOT EXISTS holding (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id       INT,
  propid        VARCHAR(32),
  buy_price     DOUBLE,
  buy_time      DATETIME,
  quantity      INT DEFAULT 1,
  status        VARCHAR(16) DEFAULT 'holding',
  list_price    DOUBLE,
  list_at       DATETIME,
  list_duration INT COMMENT '12或24',
  sell_price    DOUBLE,
  sell_at       DATETIME,
  net_profit    DOUBLE COMMENT '扣费后净收益',
  created_at    DATETIME,
  INDEX idx_user (user_id),
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='持仓';

-- 日内基准
CREATE TABLE IF NOT EXISTS baseline (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  propid      VARCHAR(32),
  slot        TINYINT COMMENT '0~23 小时',
  p20         DOUBLE,
  p50         DOUBLE,
  p80         DOUBLE,
  sample_days INT,
  updated_at  DATETIME,
  UNIQUE KEY uk_item_slot (propid, slot)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日内基准';

-- 信号记录
CREATE TABLE IF NOT EXISTS signal_record (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  propid        VARCHAR(32),
  ts            DATETIME,
  action        VARCHAR(8),
  score         INT,
  price         DOUBLE,
  expected_gain DOUBLE,
  reason        VARCHAR(500),
  created_at    DATETIME,
  INDEX idx_prop_ts (propid, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='信号记录';

-- 管理员
CREATE TABLE IF NOT EXISTS admin_user (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  username       VARCHAR(64) UNIQUE,
  password_hash  VARCHAR(255),
  role           VARCHAR(16) DEFAULT 'admin',
  last_login_at  DATETIME,
  created_at     DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='管理员';

-- 系统配置（一切可配置）
CREATE TABLE IF NOT EXISTS sys_config (
  config_key   VARCHAR(64) PRIMARY KEY,
  config_value TEXT,
  config_type  VARCHAR(16) DEFAULT 'string',
  description  VARCHAR(255),
  updated_at   DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统配置';

-- 初始配置
INSERT IGNORE INTO sys_config (config_key, config_value, config_type, description, updated_at) VALUES
('fee_deposit_rate',   '3',  'number', '保证金比例%', NOW()),
('fee_rate_12h',       '5',  'number', '12小时档手续费%', NOW()),
('fee_rate_24h',       '8',  'number', '24小时档手续费%', NOW()),
('strategy_min_gain',  '15', 'number', '最小预期收益%门槛', NOW()),
('strategy_baseline_days','14','number','日内基准回看天数', NOW()),
('strategy_buy_percentile','25','number','买入分位阈值%', NOW()),
('strategy_sell_pos',  '75', 'number','卖出周内位置阈值%', NOW()),
('strategy_slot_count','15', 'number','寄售槽位总数', NOW()),
('collect_enabled',    '1',  'bool',   '是否开启定时采集', NOW()),
('collect_interval',   '60', 'number', '采集间隔(分钟)', NOW()),
('collect_concurrency','1',  'number', '并发数(建议1)', NOW()),
('collect_delay_ms',   '1000','number','单次请求间隔毫秒', NOW()),
('ams_base_url',       'https://comm.ams.game.qq.com/ide/', 'string', 'AMS接口地址', NOW()),
('ams_chart_id',       '329101', 'string', 'AMS图表ID', NOW()),
('ams_sub_chart_id',   '329101', 'string', 'AMS子图表ID', NOW()),
('ams_ide_token',      'qkJMkB', 'string', 'AMS sIdeToken', NOW()),
('ams_appid',          '101491592', 'string', 'AMS appid', NOW()),
('data_source',        'official', 'string', '当前数据源', NOW());
