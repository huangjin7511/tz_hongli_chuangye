<!--
 * @文件路径         : \tz_hongli_chuangye\README.md
 * @作者名称         : kinve
 * @文件版本         : V1.0.0
 * @创建日期         : 2026-08-06 15:41:49
 * @简要说明         : 
 * 
 * 版权信息         : 2026 by ${git_name}, All Rights Reserved.
-->
# 创业板 / 中证红利 轮动策略

基于 GitHub Actions 的每日定时量化策略系统，通过比较创业板指数与中证红利指数的比值进行动态仓位管理。

## 📊 策略原理

- **R 值** = 创业板指数价格 ÷ 中证红利指数价格
- R 值默认在 **0.35 ~ 0.60** 区间波动
- 当 **R < 0.35**：切换至创业板（成长风格）
- 当 **R > 0.60**：切换至中证红利（价值风格）
- 区间内：按比例分配仓位

## 🏗️ 仓位管理

- **9 档仓位**，每档约 11.1%
- 1 档：全仓中证红利
- 9 档：全仓创业板
- 支持自定义阈值区间（自动回测优化）

## 📁 项目结构

```
├── .github/workflows/daily_strategy.yml   # GitHub Actions 定时任务
├── data/
│   ├── history.json                       # 历史数据
│   └── signals.json                       # 信号记录
├── config.json                            # 策略配置（阈值等）
├── index.html                             # 网页展示页面
├── strategy.py                            # 策略核心脚本
├── requirements.txt                       # Python 依赖
└── README.md
```

## 🚀 部署步骤

### 1. Fork 本仓库

### 2. 配置 GitHub Pages

在仓库 **Settings → Pages** 中设置 Source 为 `GitHub Actions`。

### 3. 配置企业微信 Webhook（可选）

在仓库 **Settings → Secrets and variables → Actions** 中添加：

- `WECHAT_WEBHOOK_URL`：企业微信群机器人 Webhook 地址

### 4. 启用 Actions

在 **Actions** 标签页手动触发一次工作流，或等待每日定时执行。

## ⏰ 定时规则

- **每日 14:30**（北京时间 CST）自动执行
- GitHub Actions 对应 cron: `30 6 * * *`（UTC 06:30）
- 支持手动触发（workflow_dispatch）

## 📈 阈值优化

系统内置历史回测功能：
- 积累 30 天以上数据后，每日自动回测
- 遍历多组阈值组合（0.30-0.40 / 0.55-0.70）
- 按策略总收益评分，自动更新最优阈值

## 🌐 网页展示

GitHub Actions 执行后会自动部署到 GitHub Pages，可查看：
- 实时 R 值与信号
- 9 档仓位可视化
- R 值历史走势图
- 历史信号记录表

## ⚠️ 免责声明

本项目仅供学习研究使用，不构成任何投资建议。量化交易存在风险，历史表现不代表未来收益。