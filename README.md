# 一个规划助手

一个个人任务/课程表/日程规划应用。核心是「AI 深度规划」：结合你的课程表、作息和精力高峰，将任务步骤自动分配到合适的日期与时段。

## 功能特性

- 📚 **课程表管理**：手动录入 / 批量添加 /课程时段作为排期硬约束。
- 🌅 **作息管理**：起床、睡觉、三餐、娱乐时段，精力高峰时段 —— 作为排期基准。
- ✅ **任务管理**：任务（考试/作业/项目/生活/其他）+ 具体步骤（名称 + 预估分钟）+ 粒度偏好（粗/中/细）。
- 🧠 **多智能体协作排期**：课程表 Agent、作息 Agent、任务拆解 Agent、排期 Agent、优先级 Agent、冲突检测 Agent、休息插入 Agent、汇总 Agent。
- 🔔 **持久化 & 逾期置顶**：所有未完成任务持续显示；逾期自动标记「⚠️ 已逾期」顺延。
- 🍅 **番茄钟**：任务步骤绑定 / 自由专注两种模式，环形进度条 + 自定义 15/25/30/45/60 分钟，自动记录用时。
- 📝 **子任务调整窗口**：点击任务卡片弹出；每步骤可编辑日期/时间/时长；可增删/智能重排/强制保存（超时柔性预警）。
- 💬 **对话规划助手**：右下角悬浮按钮，支持自然语言命令（如「重排任务 1」「添加步骤」），DeepSeek 理解后自动执行。
- 📅 **周视图日历 + 今日日程**：每步骤前勾选框完成进度自动更新；已完成事项删除线样式。

## 技术栈

- **后端**：Python Flask + SQLite3 + DeepSeek API（deepseek-chat 模型，可选 VL 接口）。
- **前端**：原生 HTML / CSS / JavaScript。
- **Agent 架构**：8 个轻量级 Agent 在同一进程内协作，无需外部队列。

## 安装 & 运行

```bash
# 1. 建议使用虚拟环境
python -m venv venv
# Windows: venv\Scripts\activate
# Linux / macOS: source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量（可选：开启 AI 对话与识图导入）
# 复制 .env 并写入你的 DeepSeek API Key：
echo "DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxx" > .env

# 4. 启动（手机访问请使用本机局域网 IP，例如 http://192.168.x.x:9090）
python app.py
```

启动后，使用浏览器（桌面或手机）访问 `http://<host>:9090/`。

## 项目结构

```
ddl_planner_2/
├── app.py             # Flask 后端（所有 Agent + 路由）
├── static/
│   └── index.html     # 前端单页应用
├── requirements.txt
├── .env.example       # 示例环境变量配置
└── README.md
```

首次启动时 `app.py` 会在当前目录创建 `ddl_planner.db`（SQLite 数据库），数据持久化保存，刷新页面不丢失。

## API 速览

| 方法 | 路由 | 作用 |
|---|---|---|
| GET | `/` | 前端页面 |
| GET/POST | `/api/courses`, `/api/courses/<id>` | 课程表 |
| POST | `/api/courses/batch` | 批量导入课程 |
| GET/POST | `/api/routines` | 作息管理 |
| GET/POST | `/api/tasks` | 任务列表 / 添加任务（自动排期） |
| GET | `/api/tasks/<id>` | 任务详情 + 步骤 |
| PUT | `/api/tasks/<id>/steps` | 批量更新子步骤 |
| POST | `/api/tasks/<id>/replan` | 智能重排 |
| DELETE | `/api/tasks/<id>` | 删除任务 |
| POST | `/api/tasks/step/<id>/complete` | 步骤完成状态切换 |
| GET | `/api/today` | 今日日程（课程 + 步骤 + 进度） |
| POST | `/api/pomodoro` | 记录一次番茄钟 |
| GET | `/api/pomodoro/stats` | 番茄统计（今日 / 全部） |
| POST | `/api/chat` | 对话规划助手 |
| GET | `/api/chat/history` | 对话历史 |

## 环境变量

在 `.env` 中配置：

```
DEEPSEEK_API_KEY=你的 DeepSeek API Key
```

若未配置，仍然可以正常使用核心的本地排期功能；对话助手与 AI 识图导入则会提示错误或降级。

## 使用建议

1. 先在「⚙️ 设置」Tab 配置作息和课程表（手动添加 / 或点击「+」> AI识图导入）。
2. 回到「📅 日历」Tab 查看今日日程；点击右下角「+」添加第一个任务（如「期末考试复习」）。
3. 填写几个步骤（步骤名 + 预估分钟）并保存，系统会自动排期。
4. 点击任务卡片打开调整窗口，可微调步骤时间 / 重新排期 / 强制保存。
5. 点任一任务步骤旁的番茄图标 → 绑定步骤的番茄钟开始；或去番茄 Tab 自由专注。
6. 点右下角 💬 → 用自然语言操作（「重排任务 1」「添加一个步骤」等）。

## 8 个 Agent 的职责

1. **CourseAgent**：解析并输出课程表，生成空闲时段；
2. **RoutineAgent**：解析作息，输出可用时段与精力高峰；
3. **TaskBreakdownAgent**：按用户粒度偏好拆分步骤；
4. **SchedulingAgent**：结合课程表 + 作息，将步骤分配到具体日期与时间；
5. **PriorityAgent**：根据类型、截止日期等评分；
6. **ConflictAgent**：检测步骤时间重叠；
7. **BreakInsertAgent**：相邻步骤之间自动插入休息；
8. **SummaryAgent**：生成每日完成度、进度百分比。

## License

MIT
