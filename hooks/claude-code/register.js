// devkit progress mod（Claude Code >= 2.1.287 的 hooks module）。
//
// 在输入框上方（AbovePrompt）展示当前 DevKit 流程进度与 token 用量，并提供 `/devkit` 面板（流水线 / 任务 / Token
// 三页）与 `/devkit-refresh` 命令。只在检测到 DevKit 流程（单个活跃 feature 或多个活跃 feature）时绘制进度行。
// 进度为合并口径：（已关闭步骤 + 已关闭任务）/（步骤总数 + 任务总数），任务统计来自 status JSON 的 `tasks`。
//
// 数据来源（全部只读）：
//   - 流程状态：`git rev-parse --show-toplevel` 定位仓库根，再在仓库根执行
//     `devkit-flow status --format json`；子进程一律用 argv 数组，不经 shell。
//   - token：`turn.complete` 的 usage，主 loop / 子 agent loop 分桶记账，feature 台账（含每轮记录）写入本插件的 $.store。
//   - 费用：`$.session.usage().cost.usd`（宿主 /cost 口径）。
//   - 主题：`$.config.list()` 的 theme 行与 `config.set`（只观察，不改写）；动效开关来自 `register(on, options)` 的 options。
// mod 不调用任何写流程状态的 CLI 子命令，也不读写 DevKit 配置文件。
//
// 逻辑类（按「状态对象 + 顶层函数」组织）：
//   ModEntry、ModSettings、Snapshot / StepView / FeatureBrief / TaskItem、StatusProbe、StepStates、TaskStyles、
//   Refresher、AdvanceNotifier / StepAdvance、TokenLedger / LedgerEntry / TurnRecord、ThemeSource / Palette、
//   BarModel / BarSite / BarFrames / BarAnimator、BandRenderer / Segment、SpinnerSuffix、PaneState、PaneRenderer、
//   CommandHandlers。
// 静态分析约束：Mods 静态分析要求 `$` 只能以 `$.noun.event(...)` 形式书写、只能作为第一个参数传给顶层函数，不能保存、
// 解构或传给对象方法，因此凡是用到宿主 API 的函数都以 `$` 作为第一个参数，状态放在 `register` 内创建的普通对象里。
// 定时器回调与 Button `onPress` 是在以 `$` 为首参的顶层函数（startTicker、schedule、Pane hook 处理函数 onPane）内创建的
// 箭头函数，`$` 经该函数的参数进入闭包，闭包体内再把它作为首参传给顶层函数。BandRenderer / PaneRenderer /
// SpinnerSuffix / BarFrames / PaneState 不调用宿主 API。

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

const PANE_ID = 'devkit-progress'
const CLI_NAME = 'devkit-flow'
const FALLBACK_CLI_SUFFIX = '/.config/devkit/bin/devkit-flow'

const TICK_MS = 15000
// 隐藏态（不绘制）与开关关闭态每 4 个 tick（约 60s）刷新一次。
const HIDDEN_POLL_EVERY_TICKS = 4
const GIT_TIMEOUT_MS = 5000
const STATUS_TIMEOUT_MS = 15000
// 主 loop turn.complete 最多等待刷新与记账的时长（R-9）；超时后链在后台继续。
const TURN_REFRESH_WAIT_MS = 2000
// 子 agent loop 的 token 是否计入 feature 累计与本会话合计；以 VT-3 结论定稿（T-11）。
const INCLUDE_SUBAGENT_TURNS = true

const DEFAULT_COLUMNS = 120
const SEGMENT_GAP = '  '
const FEATURE_NAME_MAX_COLUMNS = 32
const BAND_BAR_WIDTH = 12
const BAND_BAR_NARROW_WIDTH = 6
const ELLIPSIS = '…'
// Spinner 后缀中步骤名的最大列数（超出截断加 …）。
const SPINNER_NAME_MAX_COLUMNS = 20

// 进度行徽章与状态胶囊（§4.2.4 BarMode 胶囊表；配色取 ui-demo 第 2 版）。
const BADGE_TEXT = ' ◆ DEVKIT '
const BADGE_STYLE = { backgroundColor: '#1e3a8a', color: '#e0f2fe', bold: true }
const PANE_HINT_ARROW = '→ '
const PANE_HINT_COMMAND = '/devkit'
const MULTI_ITEM_GAP = '  ·  '
const BAR_MODE_RUN = 'run'
const BAR_MODE_WAIT = 'wait'
const BAR_MODE_BLOCK = 'block'
const BAR_MODE_DONE = 'done'
const CAPSULES = {
  [BAR_MODE_RUN]: { icon: '▶', label: '运行中', backgroundColor: '#0e7490', color: '#ecfeff' },
  [BAR_MODE_WAIT]: { icon: '⏸', label: '待确认', backgroundColor: '#b45309', color: '#fffbeb' },
  [BAR_MODE_BLOCK]: { icon: '✖', label: '阻塞', backgroundColor: '#b91c1c', color: '#fef2f2' },
  [BAR_MODE_DONE]: { icon: '✔', label: '全部完成', backgroundColor: '#047857', color: '#ecfdf5' },
}

// 进度条绘制方式（§4.2.7 降级链）：'raster' 动效（默认）、'static' 只画静态帧、'text' 第 1 版 Text 进度条。
// 以 VT-11 结论定稿（T-11）。
const BAR_RENDER = 'raster'
// Raster key 须在同一宿主树内唯一：AbovePrompt 会把其他 mod（含同一份代码的另一副本）的绘制嵌入本树，
// 固定 'bar' 会与之重复导致宿主拒绝整树（"Raster \"bar\" is drawn twice"）。按模块实例加随机后缀。
const BAR_KEY = 'bar-' + Math.random().toString(36).slice(2, 10)
const SITE_BAND = 'band'
const SITE_PANE = 'pane'
// 共享帧时钟：帧长 100ms；高光 35 帧一周期（前 14 帧扫过）；呼吸 24 帧一周期；推进 9 帧（前 5 帧移动前沿）。
const FRAME_MS = 100
const SWEEP_CYCLE_FRAMES = 35
const SWEEP_ACTIVE_FRAMES = 14
const SWEEP_LAST_FRAME = 13
const BREATH_CYCLE_FRAMES = 24
const PUSH_FRAMES = 9
const PUSH_MOVE_FRAMES = 5
// 进入待确认满 10 分钟后呼吸停在中间档 ▒，不再唤醒。
const BREATH_MAX_MS = 600000
const SWEEP_GLOW_MIX = 0.55
const SWEEP_GLOW_RADIUS = 1.8
const PUSH_GLOW_MIX = 0.75

// 色板（§4.2.7 J10、P14）：颜色为 0x00RRGGBB；终端默认色为 0x01000000（bit 24）。
const DEFAULT_COLOR = 0x01000000
const GLOW_COLOR = 0xffffff
const PALETTE_DARK = 'dark'
const PALETTE_LIGHT = 'light'
const PALETTE_UNKNOWN = 'unknown'
const PALETTE_SPECS = {
  [PALETTE_DARK]: { stops: ['#22d3ee', '#3b82f6', '#8b5cf6', '#d946ef'], empty: '#334155', amber: '#fbbf24', red: '#f87171' },
  [PALETTE_LIGHT]: { stops: ['#0891b2', '#2563eb', '#7c3aed', '#c026d3'], empty: '#cbd5e1', amber: '#d97706', red: '#dc2626' },
}

// 进度条字形：整格、⅛ 前沿、空格与呼吸三档。
const GLYPH_FULL = '█'
const GLYPH_EMPTY = '░'
const GLYPH_MEDIUM = '▒'
const PARTIAL_GLYPHS = ['', '▏', '▎', '▍', '▌', '▋', '▊', '▉']
const BREATH_GLYPHS = ['░', '▒', '▓']
const BREATH_AMBER_MIX = [0.5, 0.78, 1]

// 步骤推进 toast 文案（§4.2.6、接口文档 §4.4）。
const TOAST_BLOCKED_SUFFIX = ' · 已阻塞，输入 /devkit 查看'
const TOAST_WAITING_SUFFIX = ' · 等你确认，输入 /devkit 查看'
const TOAST_DONE_SUFFIX = ' 已完成'
const TOAST_ARROW = '  →  '
const TOAST_ALL_CLOSED = '✔ 全部步骤已关闭'

const LEDGER_KEY_PREFIX = 'tok::v1::'
const LEDGER_KEY_SEPARATOR = '::'
const LEDGER_EVICT_RATIO = 0.25
// 每条台账与 feature 视图保留的每轮记录（TurnRecord）上限，超出丢最旧的（§4.3.2）。
const RECENT_TURNS_MAX = 60
const TURN_BUCKET_MAIN = 'm'
const TURN_BUCKET_SUB = 's'

const TASKS_SOURCE_TASK_MD = 'task.md'
const TASKS_SOURCE_MISSING = 'missing'
const TASKS_SOURCE_INVALID = 'invalid'
const TASKS_SOURCE_UNAVAILABLE = 'unavailable'

const KIND_LOADING = 'loading'
const KIND_ABSENT = 'absent'
const KIND_IDLE = 'idle'
const KIND_ACTIVE = 'active'
const KIND_MULTI = 'multi'

const STATUS_OK = 'ok'
const STATUS_MULTIPLE = 'multiple_active_features'
const STATUS_NO_ACTIVE = 'no_active_feature'

const SWITCH_OFF_NOTE = '（常驻进度行已关闭）'
const ALL_CLOSED_TEXT = '✔ 全部步骤已关闭'
const IDLE_TEXT = '当前没有进行中的 DevKit feature。'
const ABSENT_TEXT = '当前目录不在 git 仓库中，或 devkit-flow 不可用。'
const SCOPE_NOTE_TEXT = '统计范围：feature 累计只含安装本 mod 之后、本机 Claude Code 会话在该 feature 下的用量；面板只统计对话轮次，/cost 另含宿主后台请求，面板通常略低'
const SWITCH_OFF_HINT = '，可执行 devkit-flow progress-bar configure --enable 开启'

// /devkit 面板（§4.2.6）：头部合并进度条宽 min(36, bodyColumns − 14)，小于 6 格不画。
const PANE_BAR_MAX_COLUMNS = 36
const PANE_BAR_MARGIN = 14
const MIN_BAR_COLUMNS = 6
const PANE_TAB_PIPELINE = 1
const PANE_TAB_TASKS = 2
const PANE_TAB_TOKENS = 3
const PANE_TABS = [
  { tab: PANE_TAB_PIPELINE, hotkey: '1', label: '流水线' },
  { tab: PANE_TAB_TASKS, hotkey: '2', label: '任务' },
  { tab: PANE_TAB_TOKENS, hotkey: '3', label: 'Token' },
]
const PANE_TAB_MARK = '▸ '
const PANE_SEPARATOR = '  ·  '
const PANE_SNAPSHOT_HINT = ' · 按 r 刷新'
const PANE_FOOTER_TEXT = 'Esc 关闭'  // 1/2/3 与 r 已在标签行按钮上显示，页脚只补按钮行没有的键
const PANE_FOOTER_SCROLL_TEXT = '↑↓ 滚动 · Esc 关闭'
// 非 active 面板已有「r: 刷新」按钮，底部只提示 Esc（D-24 去重）。
const PANE_FOOTER_PLAIN_TEXT = 'Esc 关闭'
// 多 feature 面板（D-24）：进行中一组 + 步骤已全部关闭一组。
const MULTI_ACTIVE_TITLE = '进行中的 DevKit feature：'
const MULTI_NONE_TEXT = '当前没有进行中的步骤。'
const MULTI_CLOSED_TITLE = '步骤已全部关闭，可执行 flow complete：'
const MULTI_ROW_BULLET = '• '
const MULTI_ROW_GAP = '  '
// 名单下钻（D-25）：前 9 行（进行中组在前、关闭组在后，连续编号）各一个 plain Button，热键 1–9；宿主自动绘制
// 「hotkey: 」前缀，label 只放 feature 名。详情页标签行追加「b: 返回」。
const DRILL_MAX = 9
const DRILL_KEY_PREFIX = 'drill:'
const DRILL_BACK_HOTKEY = 'b'
const DRILL_BACK_LABEL = '返回'
const DRILL_OVERFLOW_TEXT = '（仅前 ' + DRILL_MAX + ' 个可按数字键查看详情）'
const DRILL_FOOTER_SUFFIX = ' 查看详情 · Esc 关闭'
const DRILL_FAILED_SUFFIX = ' 的详情读取失败，按 r 刷新后重试'
// 步骤全关（current_step=null）的单 feature 视图（D-25）：进度行与面板胶囊后的提示、刷新文本后缀。
const DONE_HINT_TEXT = '可执行 flow complete'
const REFRESH_DONE_SUFFIX = '；步骤已全部关闭，' + DONE_HINT_TEXT
const PANE_RULE = '─'
// 分列行「任务部分」的 5 种取值（§4.2.6，按 tasksSource / tasksTotal）。
const TASKS_PART_NO_TASK_MD = '-（尚未生成 task.md）'
const TASKS_PART_INVALID = '-（task.md 无法解析，未计入进度）'
const TASKS_PART_EMPTY = '0/0'
const TASKS_PART_UNAVAILABLE = '-（当前 devkit-flow 未提供任务统计）'
// 流水线页：步骤 id `S<数字>` 的首个数字 → 阶段名；不匹配的归「其他」。阶段小进度条 8 格静态渐变。
const PHASE_NAMES = { 0: '准备', 1: '需求', 2: '设计', 3: '测试', 4: '规划', 5: '开发', 6: '测试', 7: '发布' }
const PHASE_OTHER = '其他'
const PHASE_NAME_COLUMNS = 6
const PHASE_BAR_COLUMNS = 8
const PHASE_DONE_TEXT = '  已完成'
// 任务页（§4.2.6 标签 2）：分段条 inline 最多 40 格、dock 为 bodyColumns − 2。
const TASKS_BAR_INLINE_MAX_COLUMNS = 40
const TASKS_UPGRADE_TEXT = '升级 devkit-flow 后可查看逐条任务'
// Token 页（§4.2.6 标签 3）：走势最多 60 格；main / sub 比例条 dock 20 格、inline 24 格。
const SPARK_MAX_COLUMNS = 60
const SPARK_GLYPHS = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']
const SPARK_PAD_GLYPH = '·'
const SESSION_BAR_DOCK_COLUMNS = 20
const SESSION_BAR_INLINE_COLUMNS = 24
const SPARK_EMPTY_TEXT = '尚无每轮记录（安装本版本之后的轮次才会记录）'

// ---------------------------------------------------------------------------
// StepStates：已关闭集合、状态图标与颜色（与 devkit-flow constants 的 DONE/SKIPPED/WAIVED 一致）
// ---------------------------------------------------------------------------

const DONE_STATES = new Set(['done', '已完成'])
const SKIPPED_STATES = new Set(['skipped', '已跳过'])
const WAIVED_STATES = new Set(['waived', '风险接受'])
const CLOSED_STATES = new Set([...DONE_STATES, ...SKIPPED_STATES, ...WAIVED_STATES])

function isClosedState(state) {
  return CLOSED_STATES.has(state)
}

function iconOfState(state) {
  if (DONE_STATES.has(state)) return '✔'
  if (WAIVED_STATES.has(state)) return '⚑'
  if (SKIPPED_STATES.has(state)) return '↷'
  if (state === 'in-progress') return '▶'
  if (state === 'waiting-confirmation') return '⏸'
  if (state === 'blocked') return '✖'
  return '·'
}

// StepStates.barModeOf：进度状态（胶囊与进度条动效共用，§4.2.4）。
function barModeOf(current) {
  if (!current) return BAR_MODE_DONE
  if (current.state === 'waiting-confirmation') return BAR_MODE_WAIT
  if (current.state === 'blocked') return BAR_MODE_BLOCK
  return BAR_MODE_RUN
}

function colorOfState(state) {
  if (DONE_STATES.has(state)) return 'green'
  if (WAIVED_STATES.has(state)) return 'magenta'
  if (SKIPPED_STATES.has(state)) return 'gray'
  if (state === 'in-progress') return 'cyan'
  if (state === 'waiting-confirmation') return 'yellow'
  if (state === 'blocked') return 'red'
  return undefined
}

// ---------------------------------------------------------------------------
// TaskStyles：任务状态原值的展示映射（图标、文字样式、分段色；不参与计数，§3.5 T13）
// ---------------------------------------------------------------------------

const TASK_STATE_RUNNING = '执行中'
const TASK_STATE_FAILED = '失败'
const TASK_STATE_DONE = '已完成'
const TASK_STYLES = {
  [TASK_STATE_DONE]: { icon: '✔', style: { color: 'green' }, segment: '#10b981' },
  [TASK_STATE_RUNNING]: { icon: '▶', style: { color: 'cyan' }, segment: '#06b6d4' },
  [TASK_STATE_FAILED]: { icon: '✖', style: { color: 'red' }, segment: '#ef4444' },
  取消: { icon: '↷', style: { color: 'gray' }, segment: '#94a3b8' },
  待执行: { icon: '·', style: { dimColor: true }, segment: '#475569' },
}
// 其他原值（含英文别名、空串）：默认色的 ·，分段为灰色。
const TASK_STYLE_OTHER = { icon: '·', style: {}, segment: '#94a3b8' }

function taskStyleOf(state) {
  return Object.prototype.hasOwnProperty.call(TASK_STYLES, state) ? TASK_STYLES[state] : TASK_STYLE_OTHER
}

// TaskStyles.iconOf
function taskIconOf(state) {
  return taskStyleOf(state).icon
}

// TaskStyles.colorOf：图标的 Text 样式。
function taskColorOf(state) {
  return taskStyleOf(state).style
}

// TaskStyles.segmentColorOf：分段条与图例 ■ 的颜色（0x00RRGGBB）。
function taskSegmentColorOf(state) {
  return parseInt(taskStyleOf(state).segment.slice(1), 16)
}

// TaskStyles.isRunning
function isRunningTask(state) {
  return state === TASK_STATE_RUNNING
}

// TaskStyles.isFailed
function isFailedTask(state) {
  return state === TASK_STATE_FAILED
}

// ---------------------------------------------------------------------------
// Snapshot / StepView / FeatureBrief：一次刷新得到的流程状态快照（值对象）
// ---------------------------------------------------------------------------

// 计数（§4.2.4 合并口径）：stepsClosed/stepsTotal 只计流水线步骤，tasksClosed/tasksTotal/tasksSource 来自
// readTasks；closed/total = 步骤 + 任务。byState/taskItems/itemsAvailable/itemsTruncated 来自 readTaskItems，
// 只用于面板展示，不参与任何计数。非 active 的 Snapshot 各计数为 0、tasksSource 为空串。
function createSnapshot(kind, fields) {
  return {
    kind,
    workspace: '',
    feature: '',
    featureDir: '',
    summary: {},
    current: null,
    pipeline: [],
    features: [],
    closedFeatures: [],
    stepsClosed: 0,
    stepsTotal: 0,
    tasksClosed: 0,
    tasksTotal: 0,
    tasksSource: '',
    byState: {},
    taskItems: [],
    itemsAvailable: false,
    itemsTruncated: false,
    closed: 0,
    total: 0,
    bandEnabled: true,
    switchSource: '',
    ...(fields || {}),
  }
}

// Snapshot.isVisible：multi 只有存在可计数（current_step≠null）的 feature 时才显示；
// 全部步骤已关闭的僵尸 feature 不足以显示进度行（与无流程一致，D-24）。
function isSnapshotVisible(snap) {
  if (!snap.bandEnabled) return false
  if (snap.kind === KIND_ACTIVE) return true
  return snap.kind === KIND_MULTI && snap.features.length > 0
}

// Snapshot.hasTasks：有可显示的任务统计（「任务 a/b」显示条件）。
function hasTasks(snap) {
  return snap.tasksTotal > 0
}

// Snapshot.ratio：合并进度比例，total 为 0 时为 0。
function snapshotRatio(snap) {
  return snap.total ? snap.closed / snap.total : 0
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isNonNegativeInteger(value) {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

function textOf(value) {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

function toStepView(raw) {
  if (!isPlainObject(raw)) return null
  return { id: textOf(raw.id), name: textOf(raw.name), state: textOf(raw.state), note: textOf(raw.note) }
}

function toFeatureBrief(raw) {
  if (!isPlainObject(raw)) return null
  const feature = textOf(raw.feature)
  if (!feature) return null
  return {
    feature,
    featureDir: textOf(raw.feature_dir),
    state: textOf(raw.state),
    currentStep: toStepView(raw.current_step),
    // STATUS「分支」（D-24 加性字段）；旧 CLI 无此字段时为空串，不参与分支聚焦。
    branch: textOf(raw.branch).trim(),
  }
}

function switchFieldsOf(data) {
  const summary = data.progress_bar
  if (!isPlainObject(summary)) return { bandEnabled: true, switchSource: '' }
  return { bandEnabled: summary.enabled !== false, switchSource: textOf(summary.source) }
}

// StatusProbe.readTasks：status JSON `tasks` → 计入合并进度的任务计数（§4.2.4、接口文档 §3.4）。
// 合法且 source=task.md 才计数；source=missing/invalid 按 0/0 并保留来源；字段缺失（旧 CLI）、形状非法
// 或未知 source 一律按 unavailable。不判定任务状态词（已关闭判定只在 CLI 侧）。
function readTasks(data) {
  const unavailable = { closed: 0, total: 0, source: TASKS_SOURCE_UNAVAILABLE }
  const tasks = data.tasks
  if (!isPlainObject(tasks)) return unavailable
  const source = tasks.source
  if (source === TASKS_SOURCE_MISSING || source === TASKS_SOURCE_INVALID) return { closed: 0, total: 0, source }
  if (source !== TASKS_SOURCE_TASK_MD) return unavailable
  const { total, closed } = tasks
  if (!isNonNegativeInteger(total) || !isNonNegativeInteger(closed) || closed > total) return unavailable
  return { closed, total, source: TASKS_SOURCE_TASK_MD }
}

// TaskItem：{ id, desc, state }，三者均为字符串才保留。
function toTaskItem(raw) {
  if (!isPlainObject(raw)) return null
  const { id, desc, state } = raw
  if (typeof id !== 'string' || typeof desc !== 'string' || typeof state !== 'string') return null
  return { id, desc, state }
}

// StatusProbe.readTaskItems：面板展示用的逐条任务与按状态计数（不参与计数）。
function readTaskItems(tasks) {
  const result = { byState: {}, taskItems: [], itemsAvailable: false, itemsTruncated: false }
  if (!isPlainObject(tasks)) return result
  if (isPlainObject(tasks.by_state)) {
    for (const [state, count] of Object.entries(tasks.by_state)) {
      if (isNonNegativeInteger(count)) result.byState[state] = count
    }
  }
  if (Array.isArray(tasks.items)) {
    result.itemsAvailable = true
    result.taskItems = tasks.items.map(toTaskItem).filter(Boolean)
  }
  result.itemsTruncated = tasks.items_truncated === true
  return result
}

// StatusProbe.classify：status JSON → Snapshot（§4.2.3 状态判定表）。
function classifyStatus(data, workspace) {
  if (!isPlainObject(data)) return createSnapshot(KIND_ABSENT)
  const status = data.status
  if (status === STATUS_OK && typeof data.feature === 'string' && data.feature !== '') {
    const pipeline = (Array.isArray(data.pipeline) ? data.pipeline : []).map(toStepView).filter(Boolean)
    const summary = isPlainObject(data.summary) ? data.summary : {}
    const stepsClosed = pipeline.filter((step) => isClosedState(step.state)).length
    const stepsTotal = pipeline.length
    const tasks = readTasks(data)
    return createSnapshot(KIND_ACTIVE, {
      workspace,
      feature: data.feature,
      featureDir: textOf(data.feature_dir),
      summary,
      current: toStepView(data.current_step),
      pipeline,
      stepsClosed,
      stepsTotal,
      tasksClosed: tasks.closed,
      tasksTotal: tasks.total,
      tasksSource: tasks.source,
      ...readTaskItems(data.tasks),
      closed: stepsClosed + tasks.closed,
      total: stepsTotal + tasks.total,
      ...switchFieldsOf(data),
    })
  }
  if (status === STATUS_MULTIPLE) {
    const briefs = (Array.isArray(data.features) ? data.features : []).map(toFeatureBrief).filter(Boolean)
    // 名单与「N 个进行中」只计 current_step≠null；步骤全关（未 flow complete）的另列一组（D-24）。
    const features = briefs.filter((item) => item.currentStep !== null)
    const closedFeatures = briefs.filter((item) => item.currentStep === null)
    return createSnapshot(KIND_MULTI, { workspace, features, closedFeatures, ...switchFieldsOf(data) })
  }
  if (status === STATUS_NO_ACTIVE) return createSnapshot(KIND_IDLE, { workspace })
  return createSnapshot(KIND_ABSENT)
}

// ---------------------------------------------------------------------------
// StatusProbe：定位 git 根与 CLI，执行 status，分类为 Snapshot
// ---------------------------------------------------------------------------

function createStatusProbe() {
  // cliPath：首个「子进程正常返回」的 CLI 路径，后续刷新复用；null 表示未定位。
  // chain：status 子进程串行链（D-25）：刷新探测与面板下钻读取都经 withProbe 排队，任一时刻至多一个 status 在途。
  return { cliPath: null, chain: Promise.resolve() }
}

// StatusProbe.withProbe：把 task 排进串行链；返回 task 的结果（异常照常抛给调用方，链本身不断）。
function withProbe(probe, task) {
  const p = probe.chain.then(task)
  probe.chain = p.catch(() => {})
  return p
}

async function nowMs($) {
  try {
    const value = await $.clock.now()
    return typeof value === 'number' && Number.isFinite(value) ? value : 0
  } catch {
    return 0
  }
}

// StatusProbe.isFastFailure：reject 且耗时 < timeoutMs 视为 spawn 失败（命令不存在等），否则视为超时。
function isFastFailure(elapsedMs) {
  return elapsedMs < STATUS_TIMEOUT_MS
}

// StatusProbe.resetCliPath
function resetCliPath(probe) {
  probe.cliPath = null
}

// StatusProbe.resolveGitRoot：失败、非 0 退出或输出为空时返回 null。
async function resolveGitRoot($) {
  try {
    const result = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { timeoutMs: GIT_TIMEOUT_MS })
    if (!result || result.exitCode !== 0) return null
    const root = textOf(result.stdout).trim()
    return root || null
  } catch {
    return null
  }
}

// StatusProbe.fallbackCliPath：<HOME>/.devkit/bin/devkit-flow；取不到 HOME 时返回 null。
async function fallbackCliPath($) {
  try {
    const home = await $.env.get('HOME')
    if (typeof home !== 'string' || home === '') return null
    return home.replace(/\/+$/, '') + FALLBACK_CLI_SUFFIX
  } catch {
    return null
  }
}

function parseStatusOutput(result) {
  if (!result || result.exitCode !== 0) return null
  try {
    const data = JSON.parse(textOf(result.stdout))
    return isPlainObject(data) ? data : null
  } catch {
    return null
  }
}

// StatusProbe.runWithCli：outcome = returned（子进程正常返回，不论退出码）| fast（快速失败）| timeout。
// extraArgs 插在 status 与 --format 之间（分支聚焦时为 ['--feature', name]）。
async function runWithCli($, cli, workspace, extraArgs = []) {
  const startedAt = await nowMs($)
  try {
    const result = await $.process.run([cli, 'status', ...extraArgs, '--format', 'json'], {
      cwd: workspace,
      timeoutMs: STATUS_TIMEOUT_MS,
    })
    return { outcome: 'returned', data: parseStatusOutput(result) }
  } catch {
    const elapsedMs = (await nowMs($)) - startedAt
    return { outcome: isFastFailure(elapsedMs) ? 'fast' : 'timeout', data: null }
  }
}

// StatusProbe.runStatus：复用缓存路径；缓存路径快速失败则清缓存并在本次刷新内重新定位一次。
async function runStatus($, probe, workspace) {
  if (probe.cliPath !== null) {
    const cached = await runWithCli($, probe.cliPath, workspace)
    if (cached.outcome !== 'fast') return cached.data
    resetCliPath(probe)
  }
  const candidates = [CLI_NAME]
  const fallback = await fallbackCliPath($)
  if (fallback !== null && fallback !== CLI_NAME) candidates.push(fallback)
  for (const cli of candidates) {
    const attempt = await runWithCli($, cli, workspace)
    if (attempt.outcome === 'returned') {
      probe.cliPath = cli
      return attempt.data
    }
    if (attempt.outcome === 'timeout') return null
  }
  return null
}

// StatusProbe.currentBranch：`git branch --show-current`（argv，不经 shell）；失败、非 0 或空输出（detached HEAD）为 null。
async function currentBranch($, workspace) {
  try {
    const result = await $.process.run(['git', 'branch', '--show-current'], { cwd: workspace, timeoutMs: GIT_TIMEOUT_MS })
    if (!result || result.exitCode !== 0) return null
    const branch = textOf(result.stdout).trim()
    return branch || null
  } catch {
    return null
  }
}

// StatusProbe.fetchFeature：`status --feature <名称>`（复用已定位的 CLI 路径）；结果为 ok 且 feature 一致时返回 active
// Snapshot，否则（未定位、非 0、非法 JSON、别的 feature、非 ok）返回 null。调用方负责串行（同一 runOnce 内或 withProbe）。
async function fetchFeature($, probe, workspace, name) {
  if (probe.cliPath === null) return null
  const attempt = await runWithCli($, probe.cliPath, workspace, ['--feature', name])
  if (attempt.data === null) return null
  const snap = classifyStatus(attempt.data, workspace)
  return snap.kind === KIND_ACTIVE && snap.feature === name ? snap : null
}

// StatusProbe.focusTarget（D-25，修正 D-24）：multi 时选聚焦对象，返回 feature 名或空串（不聚焦）。
// ① 当前分支对应的 feature（含 current_step=null 者）恰 1 个 → 它；② 否则可计数（current_step≠null）的恰 1 个 → 它；
// ③ 否则空串（名单）。没有任何条目带 branch（旧 CLI）时不跑 git，直接走 ②。
async function focusTarget($, workspace, multi) {
  const briefs = [...multi.features, ...multi.closedFeatures]
  if (briefs.some((item) => item.branch !== '')) {
    const branch = await currentBranch($, workspace)
    if (branch !== null) {
      const matches = briefs.filter((item) => item.branch !== '' && item.branch === branch)
      if (matches.length === 1) return matches[0].feature
    }
  }
  return multi.features.length === 1 ? multi.features[0].feature : ''
}

// StatusProbe.focus（D-25）：按 focusTarget 以 `status --feature` 取单 feature 数据；取数失败一律沿用名单（不改选 ②，
// 避免瞬时失败让进度行在两个 feature 之间跳动）。全程在同一次 runOnce 内顺序执行，status 进程至多一个在途。
async function focusMulti($, probe, workspace, multi) {
  const name = await focusTarget($, workspace, multi)
  if (name === '') return multi
  const focused = await fetchFeature($, probe, workspace, name)
  return focused === null ? multi : focused
}

// StatusProbe.probe：git 根失败即 absent，且不调用 status；multi 时按 D-25 聚焦顺序尝试聚焦。
async function probeSnapshot($, probe) {
  const workspace = await resolveGitRoot($)
  if (workspace === null) return createSnapshot(KIND_ABSENT)
  const data = await runStatus($, probe, workspace)
  if (data === null) return createSnapshot(KIND_ABSENT)
  const snap = classifyStatus(data, workspace)
  return snap.kind === KIND_MULTI ? focusMulti($, probe, workspace, snap) : snap
}

// ---------------------------------------------------------------------------
// TokenBucket / LedgerEntry
// ---------------------------------------------------------------------------

function emptyBucket() {
  return { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, turns: 0 }
}

function countOf(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 0
}

function normalizeBucket(raw) {
  if (!isPlainObject(raw)) return emptyBucket()
  return {
    input: countOf(raw.input),
    output: countOf(raw.output),
    cacheRead: countOf(raw.cacheRead),
    cacheWrite: countOf(raw.cacheWrite),
    turns: countOf(raw.turns),
  }
}

// TokenBucket.add：把一轮 usage 累加进桶（缺失字段按 0），turns += 1。
function addUsageToBucket(bucket, usage) {
  bucket.input += countOf(usage.input_tokens)
  bucket.output += countOf(usage.output_tokens)
  bucket.cacheRead += countOf(usage.cache_read_input_tokens)
  bucket.cacheWrite += countOf(usage.cache_creation_input_tokens)
  bucket.turns += 1
  return bucket
}

// TokenBucket.merge
function mergeBucket(target, other) {
  target.input += other.input
  target.output += other.output
  target.cacheRead += other.cacheRead
  target.cacheWrite += other.cacheWrite
  target.turns += other.turns
  return target
}

// TokenBucket.total：四项 token 之和（不含 turns）。
function bucketTotal(bucket) {
  return bucket.input + bucket.output + bucket.cacheRead + bucket.cacheWrite
}

// ---------------------------------------------------------------------------
// TurnRecord：每轮记录 { at, b: 'm' | 's', n }，n 与 feature 累计同口径（四项 token 之和）
// ---------------------------------------------------------------------------

// TurnRecord.fromUsage
function turnRecordFromUsage(usage, isSubagent, at) {
  const n =
    countOf(usage.input_tokens) +
    countOf(usage.output_tokens) +
    countOf(usage.cache_read_input_tokens) +
    countOf(usage.cache_creation_input_tokens)
  return { at, b: isSubagent ? TURN_BUCKET_SUB : TURN_BUCKET_MAIN, n }
}

// TurnRecord.isValid：at、n 为有限数且 n >= 0，b 为 'm' 或 's'。
function isValidTurnRecord(raw) {
  return (
    isPlainObject(raw) &&
    typeof raw.at === 'number' &&
    Number.isFinite(raw.at) &&
    typeof raw.n === 'number' &&
    Number.isFinite(raw.n) &&
    raw.n >= 0 &&
    (raw.b === TURN_BUCKET_MAIN || raw.b === TURN_BUCKET_SUB)
  )
}

function copyTurnRecord(turn) {
  return { at: turn.at, b: turn.b, n: turn.n }
}

// 只保留最后 RECENT_TURNS_MAX 条（原地）。
function trimRecent(list) {
  if (list.length > RECENT_TURNS_MAX) list.splice(0, list.length - RECENT_TURNS_MAX)
  return list
}

// ---------------------------------------------------------------------------
// LedgerEntry：{ main, sub, recent, updatedAt }
// ---------------------------------------------------------------------------

function emptyLedgerEntry() {
  return { main: emptyBucket(), sub: emptyBucket(), recent: [], updatedAt: 0 }
}

// LedgerEntry.normalize：recent 非数组按 []，丢弃非法元素，最多保留最后 60 条（旧版本条目无 recent）。
function normalizeLedgerEntry(raw) {
  if (!isPlainObject(raw)) return emptyLedgerEntry()
  const recent = Array.isArray(raw.recent) ? raw.recent.filter(isValidTurnRecord).map(copyTurnRecord) : []
  return {
    main: normalizeBucket(raw.main),
    sub: normalizeBucket(raw.sub),
    recent: trimRecent(recent),
    updatedAt: countOf(raw.updatedAt),
  }
}

// LedgerEntry.appendTurn：追加一轮记录，超过 60 条丢最旧的。
function appendTurn(entry, turn) {
  entry.recent.push(turn)
  trimRecent(entry.recent)
}

// 按 INCLUDE_SUBAGENT_TURNS 口径取一条台账对 feature 累计的贡献。
function featureShareOf(entry) {
  const share = mergeBucket(emptyBucket(), entry.main)
  return INCLUDE_SUBAGENT_TURNS ? mergeBucket(share, entry.sub) : share
}

// ---------------------------------------------------------------------------
// TokenLedger：本会话计数（main / sub 分桶）与 $.store feature 台账
// ---------------------------------------------------------------------------

function createTokenLedger() {
  return {
    // 最近一次 syncSession 读到的会话 id（可变，初值 null 表示尚未取得）；keyOf 读它生成本会话键。
    sessionId: null,
    sessionMain: emptyBucket(),
    sessionSub: emptyBucket(),
    featureTotal: emptyBucket(),
    // featureTotal / featureRecent 对应的台账前缀；与记账 Snapshot 的前缀一致时才把增量直接追加进二者。
    featureTotalPrefix: null,
    // feature 视图的每轮记录（各会话键 recent 合并、按 at 升序的最后 60 条）。
    featureRecent: [],
    currentKey: null,
    currentEntry: null,
    opChain: Promise.resolve(),
  }
}

// TokenLedger.enqueue：串行队列；任务异常被吞掉，返回的 Promise 在任务结束时 resolve、永不 reject。
function enqueue(ledger, task) {
  const p = ledger.opChain.then(task).catch(() => {})
  ledger.opChain = p
  return p
}

// TokenLedger.prefixOf
function ledgerPrefixOf(workspace, feature) {
  return LEDGER_KEY_PREFIX + encodeURIComponent(workspace) + LEDGER_KEY_SEPARATOR + encodeURIComponent(feature) + LEDGER_KEY_SEPARATOR
}

// TokenLedger.keyOf：tok::v1::<enc ws>::<enc feature>::<当前 sessionId>
function ledgerKeyOf(ledger, workspace, feature) {
  return ledgerPrefixOf(workspace, feature) + ledger.sessionId
}

// TokenLedger.loadEntry：从 $.store 载入本会话键，不存在或格式不符则新建空条目。
async function loadLedgerEntry($, key) {
  const raw = await $.store.get(key)
  return normalizeLedgerEntry(raw)
}

// TokenLedger.evictOldest：按 updatedAt 升序删除最旧 25% 的 tok::v1:: 键。
async function evictOldestLedgerKeys($) {
  const keys = (await $.store.keys()).filter((key) => typeof key === 'string' && key.startsWith(LEDGER_KEY_PREFIX))
  if (keys.length === 0) return
  const aged = []
  for (const key of keys) {
    let updatedAt = 0
    try {
      updatedAt = normalizeLedgerEntry(await $.store.get(key)).updatedAt
    } catch {
      updatedAt = 0
    }
    aged.push({ key, updatedAt })
  }
  aged.sort((left, right) => left.updatedAt - right.updatedAt)
  const count = Math.max(1, Math.ceil(aged.length * LEDGER_EVICT_RATIO))
  for (const item of aged.slice(0, count)) {
    try {
      await $.store.delete(item.key)
    } catch {
      // 单个键删除失败不影响其余淘汰。
    }
  }
}

// LedgerEntry.serialize：整条写出，必须含 recent（每条 { at, b, n } 的新数组），否则每轮记录不会落盘。
function serializeLedgerEntry(entry) {
  return {
    main: { ...entry.main },
    sub: { ...entry.sub },
    recent: entry.recent.map(copyTurnRecord),
    updatedAt: entry.updatedAt,
  }
}

// 整条写回本会话键；超限 reject 时淘汰最旧 25% 后重试一次，仍失败则抛出（由 enqueue 吞掉，放弃本轮写入）。
async function writeLedgerEntry($, key, entry) {
  try {
    await $.store.set(key, serializeLedgerEntry(entry))
  } catch {
    await evictOldestLedgerKeys($)
    await $.store.set(key, serializeLedgerEntry(entry))
  }
}

// 读取会话 id：非空字符串才算取得；reject 或空值返回 null。
async function readSessionId($) {
  try {
    const id = await $.session.id()
    return typeof id === 'string' && id !== '' ? id : null
  } catch {
    return null
  }
}

// TokenLedger.syncSession（只在队列任务内调用，不单独入队）：会话 id 与缓存比较，变化时（模块加载后第一次、
// /clear 或 REPL 内 resume 换了 id）清空会话桶与 currentKey/currentEntry，再按新 id 对台账键求和覆盖会话桶。
// 取 id 失败时沿用缓存 id；返回值表示本任务是否有可用的会话 id（D-18：无缓存 id 时 record 整轮不计）。
// featureTotal / featureTotalPrefix / featureRecent 是 feature 视图，不随会话切换清空。
async function syncSessionTask($, ledger) {
  const id = await readSessionId($)
  if (id === null || id === ledger.sessionId) return ledger.sessionId !== null
  ledger.sessionId = id
  ledger.sessionMain = emptyBucket()
  ledger.sessionSub = emptyBucket()
  ledger.currentKey = null
  ledger.currentEntry = null
  const suffix = LEDGER_KEY_SEPARATOR + id
  try {
    const main = emptyBucket()
    const sub = emptyBucket()
    for (const key of await $.store.keys()) {
      if (typeof key !== 'string' || !key.startsWith(LEDGER_KEY_PREFIX) || !key.endsWith(suffix)) continue
      const entry = normalizeLedgerEntry(await $.store.get(key))
      mergeBucket(main, entry.main)
      mergeBucket(sub, entry.sub)
    }
    ledger.sessionMain = main
    ledger.sessionSub = sub
  } catch {
    // keys / get 异常：会话计数保持空桶。
  }
  return true
}

// TokenLedger.restoreSession：入队一个只执行 syncSession 的任务（session.start 第一条语句同步调用）。
function restoreSession($, ledger) {
  return enqueue(ledger, () => restoreSessionTask($, ledger))
}

async function restoreSessionTask($, ledger) {
  await syncSessionTask($, ledger)
}

// TokenLedger.record：本会话计数累加；仅 active Snapshot 写台账。无可用会话 id 时整轮不计（D-18）。
function recordUsage($, ledger, usage, isSubagent, snap) {
  return enqueue(ledger, () => recordUsageTask($, ledger, usage, isSubagent, snap))
}

async function recordUsageTask($, ledger, usage, isSubagent, snap) {
  if (!(await syncSessionTask($, ledger))) return
  addUsageToBucket(isSubagent ? ledger.sessionSub : ledger.sessionMain, usage)
  if (snap.kind !== KIND_ACTIVE) return
  const key = ledgerKeyOf(ledger, snap.workspace, snap.feature)
  if (key !== ledger.currentKey) {
    ledger.currentEntry = await loadLedgerEntry($, key)
    ledger.currentKey = key
  }
  const entry = ledger.currentEntry
  const now = await nowMs($)
  addUsageToBucket(isSubagent ? entry.sub : entry.main, usage)
  const turn = turnRecordFromUsage(usage, isSubagent, now)
  appendTurn(entry, turn)
  // featureTotalPrefix 防护：featureTotal 仍属于上一个 feature 或尚未载入时两者都不动，由下一次 reloadFeatureTotal 重算。
  if (ledger.featureTotalPrefix === ledgerPrefixOf(snap.workspace, snap.feature)) {
    if (!isSubagent || INCLUDE_SUBAGENT_TURNS) addUsageToBucket(ledger.featureTotal, usage)
    ledger.featureRecent.push(copyTurnRecord(turn))
    trimRecent(ledger.featureRecent)
  }
  entry.updatedAt = now
  await writeLedgerEntry($, key, entry)
}

// TokenLedger.mergeRecent：合并各条目的 recent，按 at 升序（稳定）取最后 60 条。
function mergeRecent(entries) {
  const merged = []
  for (const entry of entries) for (const turn of entry.recent) merged.push(copyTurnRecord(turn))
  merged.sort((left, right) => left.at - right.at)
  return trimRecent(merged)
}

// TokenLedger.reloadFeatureTotal：同 feature 前缀各会话键求和并合并每轮记录；本会话键直接用内存条目（含未落盘增量）。
function reloadFeatureTotal($, ledger, snap) {
  return enqueue(ledger, () => reloadFeatureTotalTask($, ledger, snap))
}

async function reloadFeatureTotalTask($, ledger, snap) {
  await syncSessionTask($, ledger)
  if (snap.kind !== KIND_ACTIVE) {
    ledger.featureTotal = emptyBucket()
    ledger.featureTotalPrefix = null
    ledger.featureRecent = []
    return
  }
  const view = await featureViewOf($, ledger, snap)
  ledger.featureTotal = view.total
  ledger.featureRecent = view.recent
  ledger.featureTotalPrefix = view.prefix
}

// TokenLedger.featureViewOf（只在队列任务内调用）：active Snapshot 所属 feature 的累计与每轮记录（各会话键求和、
// 本会话键用内存条目），只读、不改 ledger 的 feature 视图。
async function featureViewOf($, ledger, snap) {
  const prefix = ledgerPrefixOf(snap.workspace, snap.feature)
  const ownKey = ledger.currentKey !== null && ledger.currentKey.startsWith(prefix) ? ledger.currentKey : null
  const keys = await $.store.keys()
  const entries = []
  for (const key of keys) {
    if (typeof key !== 'string' || !key.startsWith(prefix) || key === ownKey) continue
    entries.push(normalizeLedgerEntry(await $.store.get(key)))
  }
  if (ownKey !== null && ledger.currentEntry !== null) entries.push(ledger.currentEntry)
  const total = emptyBucket()
  for (const entry of entries) mergeBucket(total, featureShareOf(entry))
  return { total, recent: mergeRecent(entries), prefix }
}

// TokenLedger.loadFeatureView（D-25 面板下钻）：入队读取某个 active Snapshot 的 feature 视图，不影响进度行的
// featureTotal / featureRecent 与记账归属；失败时 resolve 为 null。
function loadFeatureView($, ledger, snap) {
  return enqueue(ledger, () => loadFeatureViewTask($, ledger, snap)).then((view) => view || null)
}

async function loadFeatureViewTask($, ledger, snap) {
  await syncSessionTask($, ledger)
  return featureViewOf($, ledger, snap)
}

// TokenLedger.featureTotalBucket
function featureTotalBucket(ledger) {
  return ledger.featureTotal
}

// TokenLedger.recentTurns：feature 视图的每轮记录（INCLUDE_SUBAGENT_TURNS 的过滤在 Token 页绘制时做）。
function recentTurns(ledger) {
  return ledger.featureRecent
}

// TokenLedger.sessionTotal
function sessionTotal(ledger) {
  return bucketTotal(ledger.sessionMain) + (INCLUDE_SUBAGENT_TURNS ? bucketTotal(ledger.sessionSub) : 0)
}

// TokenLedger.sessionBuckets
function sessionBuckets(ledger) {
  return { main: ledger.sessionMain, sub: ledger.sessionSub }
}

// ---------------------------------------------------------------------------
// Refresher：刷新调度（可等待的 refresh、合并去重与补跑）、定时 tick、turn.complete
// ---------------------------------------------------------------------------

function createRefresher() {
  const initial = createSnapshot(KIND_LOADING)
  return {
    snap: initial,
    // 上一次刷新前的 Snapshot；模块加载后第一个结果的 previous 为 loading（推进判定视为不可比较）。
    previous: initial,
    inflight: null,
    followUp: null,
    tick: 0,
    costUsd: null,
  }
}

function invalidateRender($) {
  try {
    $.ui.invalidate('ui.render')
  } catch {
    // 重绘请求失败不影响状态。
  }
}

async function readCostUsd($) {
  try {
    const usage = await $.session.usage()
    const usd = usage && usage.cost ? usage.cost.usd : undefined
    return typeof usd === 'number' && Number.isFinite(usd) ? usd : null
  } catch {
    return null
  }
}

// AdvanceNotifier.onRefreshed：同一 feature 的两次 active 快照对比；步骤推进时 toast 一条（总开关开启时），
// 合并比例增大时给进度行登记推进动画（同步、不调度；随后的 invalidate → band render → attach 取 f0）。
// toast 在独立 try/catch 内调用：宿主抛错只丢掉这条提示。首次刷新（prev 为 loading）与切换 feature 不可比较。
function advanceOnRefreshed($, mod, prev, next) {
  const advance = diffSnapshots(prev, next)
  if (!advance.comparable) return undefined
  let pending
  if (next.bandEnabled && stepAdvanced(advance)) {
    const text = toastTextOf(advance)
    if (text) {
      try {
        pending = $.ui.toast(text)
      } catch {
        pending = undefined
      }
    }
  }
  if (ratioIncreased(advance)) pushSite(mod.animator, SITE_BAND, advance.fromRatio, advance.toRatio)
  return pending
}

// Refresher.notifyRefreshed：在独立 try/catch 中同步调用推进判定，不等待其返回的 Promise（挂 .catch 吞掉）；
// 任何异常都不改变 runOnce 的 resolve 值、也不让 runOnce reject。
function notifyRefreshed($, mod, prev, next) {
  try {
    const pending = advanceOnRefreshed($, mod, prev, next)
    if (pending && typeof pending.catch === 'function') pending.catch(() => {})
  } catch {
    // 推进提示失败不影响刷新结果。
  }
}

// Refresher.runOnce：探测 → previous/snap 前移 → 重算 feature 累计 → 读费用 → 推进判定 → invalidate。
// 只有探测这一步抛错时降级为 absent；之后的步骤整体兜底，始终 resolve 已得到的 Snapshot，不 reject。
async function runOnce($, mod) {
  let next
  try {
    next = await withProbe(mod.probe, () => probeSnapshot($, mod.probe))
  } catch {
    next = createSnapshot(KIND_ABSENT)
  }
  try {
    const refresher = mod.refresher
    const prev = refresher.snap
    refresher.previous = prev
    refresher.snap = next
    await reloadFeatureTotal($, mod.ledger, next)
    refresher.costUsd = await readCostUsd($)
    notifyRefreshed($, mod, prev, next)
    invalidateRender($)
  } catch {
    // 兜底：返回已得到的 Snapshot。
  }
  return next
}

function createFollowUp(run) {
  let resolve = null
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve, run }
}

// 启动一次 runOnce 并登记为 inflight；结束时执行补跑迁移。
function launchRun(mod, run) {
  const refresher = mod.refresher
  const p = run()
  refresher.inflight = p
  p.then(() => settleInflight(mod, p))
  return p
}

// 在途刷新结束：有 followUp 则 inflight 指向补跑、followUp 置空，补跑结果兑现原 followUp 的全部等待者；否则 inflight 置空。
function settleInflight(mod, p) {
  const refresher = mod.refresher
  if (refresher.inflight !== p) return
  const pending = refresher.followUp
  if (pending === null) {
    refresher.inflight = null
    return
  }
  refresher.followUp = null
  launchRun(mod, pending.run).then(pending.resolve)
}

// Refresher.refresh：返回的 Snapshot 一定来自本次调用之后才开始的 runOnce；任一时刻至多一个 status 进程在途。
function refresh($, mod) {
  const refresher = mod.refresher
  if (refresher.inflight === null) return launchRun(mod, () => runOnce($, mod))
  if (refresher.followUp === null) refresher.followUp = createFollowUp(() => runOnce($, mod))
  return refresher.followUp.promise
}

// Refresher.shouldPollThisTick
function shouldPollThisTick(refresher) {
  return isSnapshotVisible(refresher.snap) || refresher.tick % HIDDEN_POLL_EVERY_TICKS === 0
}

// Refresher.onTick
function onTick($, mod) {
  mod.refresher.tick += 1
  if (shouldPollThisTick(mod.refresher)) refresh($, mod)
}

// 最多等待 promise ms 毫秒（计时用宿主时钟），超时后直接返回，promise 在后台继续。
async function waitAtMost($, promise, ms) {
  let timer = null
  try {
    const timeout = new Promise((done) => {
      timer = $.clock.after(ms, done)
    })
    await Promise.race([promise, timeout])
  } finally {
    if (timer !== null) {
      try {
        timer.cancel()
      } catch {
        // 计时器已触发或已取消。
      }
    }
  }
}

function turnUsageOf(e) {
  return e && isPlainObject(e.usage) ? e.usage : null
}

function isSubagentTurn(e) {
  return Boolean(e) && typeof e.agentId === 'string' && e.agentId !== ''
}

// Refresher.onTurnStart：任何 turn.start 都把「模型工作中」置真并触发调度（不等待）。TurnStartInput 无 agentId，
// 子 agent 或后台轮次的置真由下一次 band render 的 isWorking 校正。
async function onTurnStart($, mod, e, next) {
  setWorking(mod.animator, true)
  schedule($, mod.animator).catch(() => {})
  return next(e)
}

// Refresher.onTurnComplete（§4.2.2 时序约束）。
async function onTurnComplete($, mod, e, next) {
  const result = await next(e)
  try {
    const usage = turnUsageOf(e)
    if (isSubagentTurn(e)) {
      if (usage !== null) {
        const recorded = recordUsage($, mod.ledger, usage, true, mod.refresher.snap).then(() => invalidateRender($))
        await waitAtMost($, recorded, TURN_REFRESH_WAIT_MS)
      }
      return result
    }
    // 主 loop 结束：链开始前同步置「模型空闲」并触发调度（不等待，不进入记账队列）。
    setWorking(mod.animator, false)
    schedule($, mod.animator).catch(() => {})
    const chain = refresh($, mod)
      .then((snap) => (usage !== null ? recordUsage($, mod.ledger, usage, false, snap) : undefined))
      .then(() => invalidateRender($))
      .catch(() => {})
    await waitAtMost($, chain, TURN_REFRESH_WAIT_MS)
  } catch {
    // 记账与刷新失败不影响宿主对本轮结果的处理。
  }
  return result
}

// ---------------------------------------------------------------------------
// 显示宽度（East Asian Width：W / F 计 2 列，其余计 1 列）
// ---------------------------------------------------------------------------

// Unicode 15.1 EastAsianWidth 中 W 与 F 的码点区间（闭区间，成对出现）。
const WIDE_RANGES = [
  0x1100, 0x115f, 0x231a, 0x231b, 0x2329, 0x232a, 0x23e9, 0x23ec, 0x23f0, 0x23f0, 0x23f3, 0x23f3,
  0x25fd, 0x25fe, 0x2614, 0x2615, 0x2648, 0x2653, 0x267f, 0x267f, 0x2693, 0x2693, 0x26a1, 0x26a1,
  0x26aa, 0x26ab, 0x26bd, 0x26be, 0x26c4, 0x26c5, 0x26ce, 0x26ce, 0x26d4, 0x26d4, 0x26ea, 0x26ea,
  0x26f2, 0x26f3, 0x26f5, 0x26f5, 0x26fa, 0x26fa, 0x26fd, 0x26fd, 0x2705, 0x2705, 0x270a, 0x270b,
  0x2728, 0x2728, 0x274c, 0x274c, 0x274e, 0x274e, 0x2753, 0x2755, 0x2757, 0x2757, 0x2795, 0x2797,
  0x27b0, 0x27b0, 0x27bf, 0x27bf, 0x2b1b, 0x2b1c, 0x2b50, 0x2b50, 0x2b55, 0x2b55, 0x2e80, 0x2e99,
  0x2e9b, 0x2ef3, 0x2f00, 0x2fd5, 0x2ff0, 0x303e, 0x3041, 0x3096, 0x3099, 0x30ff, 0x3105, 0x312f,
  0x3131, 0x318e, 0x3190, 0x31e3, 0x31ef, 0x321e, 0x3220, 0x3247, 0x3250, 0x4dbf, 0x4e00, 0xa48c,
  0xa490, 0xa4c6, 0xa960, 0xa97c, 0xac00, 0xd7a3, 0xf900, 0xfaff, 0xfe10, 0xfe19, 0xfe30, 0xfe52,
  0xfe54, 0xfe66, 0xfe68, 0xfe6b, 0xff01, 0xff60, 0xffe0, 0xffe6, 0x16fe0, 0x16fe4, 0x16ff0, 0x16ff1,
  0x17000, 0x187f7, 0x18800, 0x18cd5, 0x18d00, 0x18d08, 0x1aff0, 0x1aff3, 0x1aff5, 0x1affb, 0x1affd, 0x1affe,
  0x1b000, 0x1b122, 0x1b132, 0x1b132, 0x1b150, 0x1b152, 0x1b155, 0x1b155, 0x1b164, 0x1b167, 0x1b170, 0x1b2fb,
  0x1f004, 0x1f004, 0x1f0cf, 0x1f0cf, 0x1f18e, 0x1f18e, 0x1f191, 0x1f19a, 0x1f200, 0x1f202, 0x1f210, 0x1f23b,
  0x1f240, 0x1f248, 0x1f250, 0x1f251, 0x1f260, 0x1f265, 0x1f300, 0x1f320, 0x1f32d, 0x1f335, 0x1f337, 0x1f37c,
  0x1f37e, 0x1f393, 0x1f3a0, 0x1f3ca, 0x1f3cf, 0x1f3d3, 0x1f3e0, 0x1f3f0, 0x1f3f4, 0x1f3f4, 0x1f3f8, 0x1f43e,
  0x1f440, 0x1f440, 0x1f442, 0x1f4fc, 0x1f4ff, 0x1f53d, 0x1f54b, 0x1f54e, 0x1f550, 0x1f567, 0x1f57a, 0x1f57a,
  0x1f595, 0x1f596, 0x1f5a4, 0x1f5a4, 0x1f5fb, 0x1f64f, 0x1f680, 0x1f6c5, 0x1f6cc, 0x1f6cc, 0x1f6d0, 0x1f6d2,
  0x1f6d5, 0x1f6d7, 0x1f6dc, 0x1f6df, 0x1f6eb, 0x1f6ec, 0x1f6f4, 0x1f6fc, 0x1f7e0, 0x1f7eb, 0x1f7f0, 0x1f7f0,
  0x1f90c, 0x1f93a, 0x1f93c, 0x1f945, 0x1f947, 0x1f9ff, 0x1fa70, 0x1fa7c, 0x1fa80, 0x1fa88, 0x1fa90, 0x1fabd,
  0x1fabf, 0x1fac5, 0x1face, 0x1fadb, 0x1fae0, 0x1fae8, 0x1faf0, 0x1faf8, 0x20000, 0x2fffd, 0x30000, 0x3fffd,
]

function isWideCodePoint(cp) {
  if (cp < WIDE_RANGES[0]) return false
  let low = 0
  let high = WIDE_RANGES.length / 2 - 1
  while (low <= high) {
    const mid = (low + high) >> 1
    if (cp < WIDE_RANGES[mid * 2]) high = mid - 1
    else if (cp > WIDE_RANGES[mid * 2 + 1]) low = mid + 1
    else return true
  }
  return false
}

// BandRenderer.displayWidth
function displayWidth(text) {
  let width = 0
  for (const ch of text) width += isWideCodePoint(ch.codePointAt(0)) ? 2 : 1
  return width
}

// 截到不超过 maxColumns 列（含末尾 …）；本身放得下则原样返回；连 … 都放不下返回空串。
function truncateToWidth(text, maxColumns) {
  if (displayWidth(text) <= maxColumns) return text
  const budget = maxColumns - displayWidth(ELLIPSIS)
  if (budget < 0) return ''
  let kept = ''
  let width = 0
  for (const ch of text) {
    const w = displayWidth(ch)
    if (width + w > budget) break
    kept += ch
    width += w
  }
  return kept + ELLIPSIS
}

// ---------------------------------------------------------------------------
// 格式化
// ---------------------------------------------------------------------------

const COUNT_UNITS = [
  [1e9, 'B'],
  [1e6, 'M'],
  [1e3, 'k'],
]

function formatScaled(value) {
  if (value >= 100) return value.toFixed(0)
  if (value >= 10) return value.toFixed(1)
  return value.toFixed(2)
}

// token 数：三位有效数字 + 单位（1.23M、345k、21.5k），小于 1000 原样。
function formatCount(n) {
  const value = countOf(n)
  for (let index = 0; index < COUNT_UNITS.length; index += 1) {
    const [base, suffix] = COUNT_UNITS[index]
    if (value < base) continue
    const text = formatScaled(value / base)
    if (Number(text) >= 1000 && index > 0) return formatScaled(value / COUNT_UNITS[index - 1][0]) + COUNT_UNITS[index - 1][1]
    return text + suffix
  }
  return String(Math.round(value))
}

function formatUsd(usd) {
  return '$' + usd.toFixed(2)
}

function percentOf(closed, total) {
  return total ? Math.round((closed / total) * 100) : 0
}

function progressCountText(closed, total) {
  return total ? closed + '/' + total + ' ' + percentOf(closed, total) + '%' : '0/0'
}

function scaleModeText(summary) {
  return [textOf(summary.scale), textOf(summary.mode)].filter(Boolean).join('·')
}

// 面板流水线页与多 feature 面板使用的步骤文本（进度行改用胶囊，见 bandStepRuns）。
function stepText(step, name) {
  return iconOfState(step.state) + ' ' + step.id + (name ? ' ' + name : '') + ' [' + step.state + ']'
}

// ---------------------------------------------------------------------------
// ModSettings：插件 userConfig（J9-B，`register(on, options)` 的 options）
// ---------------------------------------------------------------------------

// ModSettings.fromOptions：只有 options.progressAnimation === false 时关闭动效；缺失、非布尔一律按开（§3.5 T11）。
function settingsFromOptions(options) {
  return { animationEnabled: !(isPlainObject(options) && options.progressAnimation === false) }
}

// ---------------------------------------------------------------------------
// Palette / ThemeSource：按 Claude Code 主题取色板（§4.2.7 色板）
// ---------------------------------------------------------------------------

function rgbOf(hex) {
  return parseInt(hex.slice(1), 16)
}

function createPalette(mode) {
  const spec = PALETTE_SPECS[mode === PALETTE_LIGHT ? PALETTE_LIGHT : PALETTE_DARK]
  return {
    mode,
    stops: spec.stops.map(rgbOf),
    // 主题未知时空格用终端默认色，渐变与标记色用深色板。
    empty: mode === PALETTE_UNKNOWN ? DEFAULT_COLOR : rgbOf(spec.empty),
    amber: rgbOf(spec.amber),
    red: rgbOf(spec.red),
    glow: GLOW_COLOR,
  }
}

// Palette.fromTheme：字符串含 light → 浅色；其他非空字符串 → 深色；其余 → 主题未知。
function paletteFromTheme(value) {
  if (typeof value !== 'string' || value === '') return createPalette(PALETTE_UNKNOWN)
  return createPalette(value.includes('light') ? PALETTE_LIGHT : PALETTE_DARK)
}

// Palette.isKnown
function isPaletteKnown(palette) {
  return palette.mode !== PALETTE_UNKNOWN
}

// 两色按 ratio 线性混合（逐通道取整）；任一方为终端默认色时无法混合，按 ratio 取较近的一方。
function mixColor(from, to, ratio) {
  if (from === DEFAULT_COLOR || to === DEFAULT_COLOR) return ratio >= 0.5 ? to : from
  const k = Math.min(1, Math.max(0, ratio))
  let mixed = 0
  for (const shift of [16, 8, 0]) {
    const a = (from >> shift) & 0xff
    const b = (to >> shift) & 0xff
    mixed |= Math.round(a + (b - a) * k) << shift
  }
  return mixed
}

// Palette.gradientAt：t ∈ [0, 1] 在四段色之间线性插值（A1）。
function gradientAt(palette, t) {
  const stops = palette.stops
  const position = Math.min(1, Math.max(0, t)) * (stops.length - 1)
  const index = Math.min(stops.length - 2, Math.floor(position))
  return mixColor(stops[index], stops[index + 1], position - index)
}

function createThemeSource() {
  return { palette: createPalette(PALETTE_UNKNOWN) }
}

// ThemeSource.current
function themePalette(theme) {
  return theme.palette
}

// ThemeSource.load：读 $.config.list() 的 theme 行；读取失败、无该行或非字符串按主题未知。
async function loadTheme($, theme) {
  try {
    const rows = await $.config.list()
    const row = Array.isArray(rows) ? rows.find((item) => isPlainObject(item) && item.key === 'theme') : undefined
    theme.palette = paletteFromTheme(row ? row.value : undefined)
  } catch {
    theme.palette = createPalette(PALETTE_UNKNOWN)
  }
}

// ThemeSource.onConfigSet：只观察 theme 的写入结果；未被拒绝时换色板并重绘，原样返回 next(e) 的结果。
async function onThemeConfigSet($, theme, e, next) {
  const result = await next(e)
  if (isPlainObject(result) && !result.deny) {
    theme.palette = paletteFromTheme(result.value)
    invalidateRender($)
  }
  return result
}

// ---------------------------------------------------------------------------
// BarFrames：进度条帧生成与编码（纯函数，不读时钟、不调用宿主 API；§4.2.7「帧生成」）
// ---------------------------------------------------------------------------

function positiveModulo(value, base) {
  return ((value % base) + base) % base
}

function modelRatio(model) {
  return model.total ? model.closed / model.total : 0
}

// A2：eighths = round(比例 × 格数 × 8)；filled 为已填充格数（含前沿格）。
function fillOf(ratio, width) {
  const eighths = Math.round(Math.min(1, Math.max(0, ratio)) * width * 8)
  const full = Math.min(width, Math.floor(eighths / 8))
  const rem = full < width ? eighths % 8 : 0
  return { full, rem, filled: full + (rem > 0 ? 1 : 0) }
}

// 推进进行中（f0 已取帧且 0 ≤ frame − f0 < 9）才返回 push；空、f0 为空或已结束按无推进处理（N9-W2）。
function activePush(site, frame) {
  const push = site.push
  if (!push || push.f0 === null) return null
  const elapsed = frame - push.f0
  return elapsed >= 0 && elapsed < PUSH_FRAMES ? push : null
}

// A4：呼吸档位 0..2（░ ▒ ▓）。
function breathLevel(frame) {
  const phase = positiveModulo(frame, BREATH_CYCLE_FRAMES)
  return Math.round((1 - Math.cos((2 * Math.PI * phase) / BREATH_CYCLE_FRAMES)) / 2 * 2)
}

function breathExpired(site, frame) {
  return site.waitSinceFrame !== null && (frame - site.waitSinceFrame) * FRAME_MS >= BREATH_MAX_MS
}

function breathExpiryFrame(site) {
  return site.waitSinceFrame === null ? Infinity : site.waitSinceFrame + Math.ceil(BREATH_MAX_MS / FRAME_MS)
}

function cell(glyph, foreground) {
  return [glyph.codePointAt(0), foreground, DEFAULT_COLOR]
}

// BarFrames.frameCells：按 site.model、waitSinceFrame 与 push 输出 columns 个 [codePoint, fg, bg]。
function frameCells(site, frame, palette) {
  const model = site.model
  const width = model.columns
  // 动效关闭时只取静态帧：无高光、呼吸固定 ▒、推进不播放（直接画新位置）。
  const push = model.animate ? activePush(site, frame) : null
  const pushFrame = push ? frame - push.f0 : 0
  let ratio = modelRatio(model)
  if (push) {
    const progress = Math.min(1, pushFrame / PUSH_MOVE_FRAMES)
    const eased = 1 - (1 - progress) * (1 - progress)
    ratio = push.from + (push.to - push.from) * eased
  }
  const { full, rem, filled } = fillOf(ratio, width)
  const cells = []
  for (let index = 0; index < width; index += 1) {
    const color = gradientAt(palette, width > 1 ? index / (width - 1) : 0)
    if (index < full) cells.push(cell(GLYPH_FULL, color))
    else if (index === full && rem > 0) cells.push(cell(PARTIAL_GLYPHS[rem], color))
    else cells.push(cell(GLYPH_EMPTY, palette.empty))
  }
  if (filled < width) {
    if (model.mode === BAR_MODE_WAIT) {
      const level = model.animate && !breathExpired(site, frame) ? breathLevel(frame) : 1
      cells[filled] = cell(BREATH_GLYPHS[level], mixColor(palette.empty, palette.amber, BREATH_AMBER_MIX[level]))
    } else if (model.mode === BAR_MODE_BLOCK) {
      cells[filled] = cell(GLYPH_MEDIUM, palette.red)
    }
  }
  if (push) {
    // A6：新填充区间的已填充格由白光退回渐变；推进期间不画高光。
    const mix = PUSH_GLOW_MIX * (1 - pushFrame / PUSH_FRAMES)
    const from = Math.floor(push.from * width)
    const to = Math.min(filled, Math.ceil(push.to * width))
    for (let index = Math.max(0, from); index < to; index += 1) cells[index][1] = mixColor(cells[index][1], palette.glow, mix)
  } else if (model.animate && model.mode === BAR_MODE_RUN && model.working) {
    // A3：高光只在周期前 14 帧扫过已填充格。
    const cycle = positiveModulo(frame, SWEEP_CYCLE_FRAMES)
    if (cycle <= SWEEP_LAST_FRAME) {
      const head = -2 + ((filled + 4) * cycle) / SWEEP_LAST_FRAME
      for (let index = 0; index < filled; index += 1) {
        const k = Math.max(0, 1 - Math.abs(index - head) / SWEEP_GLOW_RADIUS)
        if (k > 0) cells[index][1] = mixColor(cells[index][1], palette.glow, SWEEP_GLOW_MIX * k)
      }
    }
  }
  return cells
}

// BarFrames.nextChangeFrame：下一次内容可能变化的帧号；无则为 Infinity（§4.2.7「调度」）。
function nextChangeFrame(site, frame) {
  const model = site.model
  if (!model.animate) return Infinity
  if (activePush(site, frame)) return frame + 1
  const { filled } = fillOf(modelRatio(model), model.columns)
  if (model.mode === BAR_MODE_RUN) {
    if (!model.working || filled === 0) return Infinity
    const cycle = positiveModulo(frame, SWEEP_CYCLE_FRAMES)
    return cycle < SWEEP_ACTIVE_FRAMES ? frame + 1 : frame + (SWEEP_CYCLE_FRAMES - cycle)
  }
  if (model.mode === BAR_MODE_WAIT) {
    if (filled >= model.columns || breathExpired(site, frame)) return Infinity
    const expiry = breathExpiryFrame(site)
    const level = breathLevel(frame)
    for (let next = frame + 1; next <= frame + BREATH_CYCLE_FRAMES; next += 1) {
      if (next >= expiry) return expiry
      if (breathLevel(next) !== level) return next
    }
  }
  return Infinity
}

const BASE64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

function base64Of(bytes) {
  let out = ''
  for (let index = 0; index < bytes.length; index += 3) {
    const a = bytes[index]
    const b = index + 1 < bytes.length ? bytes[index + 1] : 0
    const c = index + 2 < bytes.length ? bytes[index + 2] : 0
    const triple = (a << 16) | (b << 8) | c
    out += BASE64_ALPHABET[(triple >> 18) & 63] + BASE64_ALPHABET[(triple >> 12) & 63]
    out += index + 1 < bytes.length ? BASE64_ALPHABET[(triple >> 6) & 63] : '='
    out += index + 2 < bytes.length ? BASE64_ALPHABET[triple & 63] : '='
  }
  return out
}

// BarFrames.encodeCells：小端 u32 三元组 [codePoint, foreground, background] 的标准 base64。
function encodeCells(cells) {
  const bytes = new Uint8Array(cells.length * 12)
  const view = new DataView(bytes.buffer)
  cells.forEach(([codePoint, foreground, background], index) => {
    view.setUint32(index * 12, codePoint >>> 0, true)
    view.setUint32(index * 12 + 4, foreground >>> 0, true)
    view.setUint32(index * 12 + 8, background >>> 0, true)
  })
  return base64Of(bytes)
}

// BarFrames.textBar：降级第 2 级（无 Raster）的第 1 版字形，前 round(比例 × 宽度) 格 █、其余 ░。
function textBar(closed, total, width) {
  if (!total) return ''
  const filled = Math.min(width, Math.max(0, Math.round((closed / total) * width)))
  return GLYPH_FULL.repeat(filled) + GLYPH_EMPTY.repeat(width - filled)
}

// 只用于帧生成的静态站点（无推进、无呼吸起点）。
function staticSite(model) {
  return { model, waitSinceFrame: null, push: null }
}

// BarFrames.staticGradientCells：面板静态渐变 Raster 的格子（无状态标记、无动效）。
function staticGradientCells(closed, total, width, palette) {
  const model = { columns: width, closed, total, mode: BAR_MODE_DONE, waitKey: '', working: false, animate: false }
  return frameCells(staticSite(model), 0, palette)
}

// BarFrames.segmentCells：任务分段条（静态）。按 byState 键序分段，每段宽 round(计数 / total × width)，
// 最后一段取剩余格；颜色按状态原值（TaskStyles.segmentColorOf）。
function segmentCells(byState, total, width) {
  const entries = Object.entries(byState).filter(([, count]) => count > 0)
  const cells = []
  let used = 0
  entries.forEach(([state, count], index) => {
    const share = index === entries.length - 1 ? width - used : Math.round((count / (total || 1)) * width)
    const n = Math.max(0, Math.min(width - used, share))
    for (let k = 0; k < n; k += 1) cells.push(cell(GLYPH_FULL, taskSegmentColorOf(state)))
    used += n
  })
  while (cells.length < width) cells.push(cell(GLYPH_EMPTY, DEFAULT_COLOR))
  return cells
}

// Token 页走势的数据：INCLUDE_SUBAGENT_TURNS 为假时先去掉子 agent 轮（b='s'），再取最后 width 条（J12）。
function sparkTurnsOf(turns, includeSubagent, width) {
  const kept = includeSubagent ? turns : turns.filter((turn) => turn.b !== TURN_BUCKET_SUB)
  return width > 0 ? kept.slice(-width) : []
}

// BarFrames.sparkCells：每轮用量 sparkline（静态）。记录不足 width 条时左侧补 ·（空格色）；每格 ▁..█ 中第
// round(n / 峰值 × 7) 个，主 loop 轮用渐变首色、子 agent 轮用渐变第三色。
function sparkCells(turns, width, palette) {
  const shown = turns.slice(-width)
  const peak = shown.reduce((max, turn) => Math.max(max, turn.n), 0)
  const cells = []
  for (let k = shown.length; k < width; k += 1) cells.push(cell(SPARK_PAD_GLYPH, palette.empty))
  for (const turn of shown) {
    const level = peak > 0 ? Math.round((turn.n / peak) * (SPARK_GLYPHS.length - 1)) : 0
    cells.push(cell(SPARK_GLYPHS[level], turn.b === TURN_BUCKET_SUB ? palette.stops[2] : palette.stops[0]))
  }
  return cells
}

// 静态帧字形（动效关闭时各格字符），用于进度行宽度计算与 plainText（§4.2.5 示例）。
function staticBarGlyphs(closed, total, width, mode) {
  const model = { columns: width, closed, total, mode, waitKey: '', working: false, animate: false }
  return frameCells(staticSite(model), 0, createPalette(PALETTE_UNKNOWN))
    .map((item) => String.fromCodePoint(item[0]))
    .join('')
}

// ---------------------------------------------------------------------------
// BarAnimator：按站点（band / pane）保存的跨 render 状态 BarSite 与共享帧时钟。
// attach / detach / pushSite / setWorking / initEpoch / currentFrame 为同步函数、不调用宿主 API；
// 只有 schedule / wake / blitSite 以 $ 为首参使用宿主 API。
// ---------------------------------------------------------------------------

function createBarAnimator(theme) {
  return {
    sites: new Map(),
    theme,
    epoch: null,
    working: false,
    // $.clock.after 返回的帧唤醒句柄，任一时刻至多一个（与 mod.ticker 相互独立）。
    timer: null,
    waking: false,
    wakeAgain: false,
  }
}

// BarAnimator.initEpoch
function initEpoch(animator, now) {
  animator.epoch = now
}

// BarAnimator.currentFrame：floor((now − epoch) / 100)。
function currentFrame(animator, now) {
  return animator.epoch === null ? 0 : Math.floor((now - animator.epoch) / FRAME_MS)
}

// BarAnimator.attach：render 内调用，返回 Raster 的初始 cells 编码；gen 加 1，按 waitKey 保留呼吸起点，
// 推进的 f0 取首次挂载的帧号（0.9 秒内重新 attach 不重播），首次被拒宽限复位。
function attachSite(animator, name, requestId, model, frame) {
  let site = animator.sites.get(name)
  if (!site) {
    site = { requestId, gen: 0, model, lastSent: '', waitKey: '', waitSinceFrame: null, push: null, graceDeny: true }
    animator.sites.set(name, site)
  }
  site.gen += 1
  site.requestId = requestId
  site.model = model
  if (!(model.waitKey && model.waitKey === site.waitKey)) {
    site.waitKey = model.waitKey
    site.waitSinceFrame = model.waitKey ? frame : null
  }
  if (site.push) {
    if (site.push.f0 === null) site.push.f0 = frame
    else if (frame >= site.push.f0 + PUSH_FRAMES) site.push = null
  }
  site.graceDeny = true
  site.lastSent = encodeCells(frameCells(site, frame, themePalette(animator.theme)))
  return site.lastSent
}

// BarAnimator.detach：撤下站点（连同推进与呼吸起点）。
function detachSite(animator, name) {
  animator.sites.delete(name)
}

// BarAnimator.push：站点已挂载时登记推进；未挂载时丢弃（不会在之后回放）。
function pushSite(animator, name, fromRatio, toRatio) {
  const site = animator.sites.get(name)
  if (site) site.push = { from: fromRatio, to: toRatio, f0: null }
}

// BarAnimator.setWorking：pane 站点不随 turn hook 重跑 render，就地同步其 model.working。
function setWorking(animator, working) {
  animator.working = working
  const pane = animator.sites.get(SITE_PANE)
  if (pane) pane.model.working = working
}

// BarAnimator.lastWorking
function lastWorking(animator) {
  return animator.working
}

// BarAnimator.schedule：await 之后同步完成「取消旧唤醒 → 各站点最近的变化帧 → 至多挂一个 clock.after」。
// 调用方一律不等待（.catch 吞掉）；并发调用时最后完成的一次生效。
async function schedule($, animator) {
  const now = await $.clock.now()
  if (animator.timer !== null) {
    try {
      animator.timer.cancel()
    } catch {
      // 已触发或已取消。
    }
    animator.timer = null
  }
  if (typeof now !== 'number' || !Number.isFinite(now)) return
  if (animator.epoch === null) initEpoch(animator, now)
  const frame = currentFrame(animator, now)
  let next = Infinity
  for (const site of animator.sites.values()) next = Math.min(next, nextChangeFrame(site, frame))
  if (next === Infinity) return
  const delay = Math.max(1, animator.epoch + next * FRAME_MS - now)
  animator.timer = $.clock.after(delay, () => {
    wake($, animator).catch(() => {})
  })
}

// BarAnimator.wake（单飞）：执行中再次唤醒只记「再跑一轮」；逐站点 await blitSite（不并发）；
// finally 复位标志并重新调度，clock.now reject 或任一步抛错都不会让 waking 卡在真值（N9-W1）。
async function wake($, animator) {
  if (animator.waking) {
    animator.wakeAgain = true
    return
  }
  animator.waking = true
  try {
    do {
      animator.wakeAgain = false
      const now = await $.clock.now()
      const frame = currentFrame(animator, now)
      for (const name of [...animator.sites.keys()]) await blitSite($, animator, name, frame)
    } while (animator.wakeAgain)
  } finally {
    animator.waking = false
    animator.wakeAgain = false
    schedule($, animator).catch(() => {})
  }
}

// BarAnimator.blitSite：cells 与 lastSent 相同不发；返回时站点已撤下或 gen 已变则忽略结果；
// 成功更新 lastSent；被拒且有宽限只消耗宽限，无宽限则撤下站点、不重试；异常按被拒处理。
async function blitSite($, animator, name, frame) {
  const site = animator.sites.get(name)
  if (!site) return
  if (site.push && site.push.f0 !== null && frame >= site.push.f0 + PUSH_FRAMES) site.push = null
  const gen = site.gen
  const cells = encodeCells(frameCells(site, frame, themePalette(animator.theme)))
  if (cells === site.lastSent) return
  let denied
  try {
    const result = await $.ui.blit({ requestId: site.requestId, key: BAR_KEY, cells })
    denied = isPlainObject(result) && Boolean(result.deny)
  } catch {
    denied = true
  }
  if (animator.sites.get(name) !== site || site.gen !== gen) return
  if (!denied) {
    site.lastSent = cells
    return
  }
  if (site.graceDeny) {
    site.graceDeny = false
    return
  }
  detachSite(animator, name)
}

// ---------------------------------------------------------------------------
// AdvanceNotifier / StepAdvance：步骤推进 toast 与推进动画判定（§4.2.6、§4.2.7 A6）
// ---------------------------------------------------------------------------

function closedStepIds(snap) {
  return new Set(snap.pipeline.filter((step) => isClosedState(step.state)).map((step) => step.id))
}

// AdvanceNotifier.diff → StepAdvance
function diffSnapshots(prev, next) {
  const comparable =
    Boolean(prev) && Boolean(next) &&
    prev.kind === KIND_ACTIVE && next.kind === KIND_ACTIVE &&
    prev.workspace === next.workspace && prev.feature === next.feature
  if (!comparable) return { comparable: false, newlyClosed: [], breakpointChanged: false, fromRatio: 0, toRatio: 0, next }
  const before = closedStepIds(prev)
  const newlyClosed = next.pipeline.filter((step) => isClosedState(step.state) && !before.has(step.id))
  const prevKey = prev.current ? prev.current.id + '\u0000' + prev.current.state : ''
  const nextKey = next.current ? next.current.id + '\u0000' + next.current.state : ''
  return {
    comparable: true,
    newlyClosed,
    breakpointChanged: prevKey !== nextKey,
    fromRatio: snapshotRatio(prev),
    toRatio: snapshotRatio(next),
    next,
  }
}

// StepAdvance.stepAdvanced
function stepAdvanced(advance) {
  return advance.newlyClosed.length > 0 || advance.breakpointChanged
}

// StepAdvance.ratioIncreased
function ratioIncreased(advance) {
  return advance.toRatio > advance.fromRatio
}

function stepLabel(step) {
  return step.name ? step.id + ' ' + step.name : step.id
}

// AdvanceNotifier.toastText：按优先级（阻塞 → 待确认 → 单步完成 → 多步完成）取至多一条文案。
function toastTextOf(advance) {
  const current = advance.next.current
  if (advance.breakpointChanged && current && current.state === 'blocked') return '✖ ' + stepLabel(current) + TOAST_BLOCKED_SUFFIX
  if (advance.breakpointChanged && current && current.state === 'waiting-confirmation') {
    return '⏸ ' + stepLabel(current) + TOAST_WAITING_SUFFIX
  }
  const closed = advance.newlyClosed
  if (closed.length === 0) return ''
  const target = current ? iconOfState(current.state) + ' ' + stepLabel(current) : TOAST_ALL_CLOSED
  const head =
    closed.length === 1
      ? '✔ ' + stepLabel(closed[0]) + TOAST_DONE_SUFFIX
      : '✔ ' + closed.length + ' 个步骤已完成（' + closed[0].id + ELLIPSIS + closed[closed.length - 1].id + '）'
  return head + TOAST_ARROW + target
}

// ---------------------------------------------------------------------------
// SpinnerSuffix：模型工作时 Spinner 后缀「… · DevKit <图标> <id> <name>」（§4.2.6、接口文档 §4.4）
// ---------------------------------------------------------------------------

// SpinnerSuffix.suffixOf：宿主文本已以 … 结尾时不再以 … 开头，避免出现 ……。
function spinnerSuffixOf(snap, base) {
  const step = snap.current
  const name = step.name ? ' ' + truncateToWidth(step.name, SPINNER_NAME_MAX_COLUMNS) : ''
  const tail = 'DevKit ' + iconOfState(step.state) + ' ' + step.id + name
  return (typeof base === 'string' && base.endsWith(ELLIPSIS) ? ' · ' : ELLIPSIS + ' · ') + tail
}

// SpinnerSuffix.render（不调用宿主 API）
function renderSpinner(e, next, snap) {
  if (!(e && isPlainObject(e.props)) || snap.kind !== KIND_ACTIVE || !snap.bandEnabled || !snap.current) return next(e)
  const base = e.props.message ?? e.props.word
  return next({ ...e, props: { ...e.props, suffix: spinnerSuffixOf(snap, base) } })
}

// ---------------------------------------------------------------------------
// BandRenderer：AbovePrompt 进度行、丢段与绘制（§4.2.5）
// ---------------------------------------------------------------------------

// 段内的一段文字（run）：text 与 Text 样式；bar 为真时是进度条占位（文字为静态帧字形）。
function run(text, style, bar) {
  return { text, style: style || {}, bar: Boolean(bar) }
}

// Segment：{ id, group(left|right), dropOrder, runs, text, ...载荷 }；dropOrder 仅作说明，丢段顺序见 fitSegments。
function segment(id, group, dropOrder, runs, payload) {
  return { id, group, dropOrder, runs, text: runs.map((item) => item.text).join(''), ...(payload || {}) }
}

function badgeSegment() {
  return segment('S0', 'left', 0, [run(BADGE_TEXT, BADGE_STYLE)])
}

function hintSegment() {
  return segment('S4h', 'left', 1, [run(PANE_HINT_ARROW, { dimColor: true }), run(PANE_HINT_COMMAND, { underline: true })])
}

// S4d（D-25）：步骤全关（✔ 全部完成）时胶囊后的「可执行 flow complete」，与 S4h 同为最先丢弃的段。
function doneHintSegment() {
  return segment('S4d', 'left', 1, [run(DONE_HINT_TEXT, { dimColor: true })])
}

function segmentsWidth(segments) {
  if (segments.length === 0) return 0
  return segments.reduce((sum, item) => sum + displayWidth(item.text), 0) + displayWidth(SEGMENT_GAP) * (segments.length - 1)
}

// S3：<进度条> <合并进度数>[ 任务 a/b]；barWidth 为 0 时只剩合并进度数（「任务 a/b」与进度条同进同退）。
function progressSegment(closed, total, mode, count, tasksText, barWidth) {
  const runs = []
  if (barWidth > 0) {
    runs.push(run(staticBarGlyphs(closed, total, barWidth, mode), {}, true))
    runs.push(run(' ' + count + (tasksText ? ' ' + tasksText : '')))
  } else {
    runs.push(run(count))
  }
  return segment('S3', 'left', 0, runs, { closed, total, mode, count, tasksText, barWidth })
}

// S4：状态胶囊（带背景色）+ 步骤 id + 步骤名；全部关闭时只有胶囊。
function bandStepSegment(mode, step, name) {
  const capsule = CAPSULES[mode]
  const runs = [run(' ' + capsule.icon + ' ' + capsule.label + ' ', { backgroundColor: capsule.backgroundColor, color: capsule.color, bold: true })]
  if (step) runs.push(run(' ' + step.id + (name ? ' ' + name : ''), { bold: false }))
  return segment('S4', 'left', 0, runs, { mode, step })
}

// BandRenderer.buildSegments：active 快照的全部段；左组 S0 S1 S2 S3 S4 S4h，右组 S5a S5b S6。
function buildSegments(snap, ledger, cost) {
  const mode = barModeOf(snap.current)
  const segments = [badgeSegment()]
  segments.push(segment('S1', 'left', 0, [run(truncateToWidth(snap.feature, FEATURE_NAME_MAX_COLUMNS), { bold: true })]))
  const scaleMode = scaleModeText(snap.summary)
  if (scaleMode) segments.push(segment('S2', 'left', 5, [run(scaleMode, { dimColor: true })]))
  const tasksText = hasTasks(snap) ? '任务 ' + snap.tasksClosed + '/' + snap.tasksTotal : ''
  const barWidth = snap.total ? BAND_BAR_WIDTH : 0
  segments.push(progressSegment(snap.closed, snap.total, mode, progressCountText(snap.closed, snap.total), tasksText, barWidth))
  segments.push(bandStepSegment(mode, snap.current, snap.current ? snap.current.name : ''))
  if (mode === BAR_MODE_WAIT || mode === BAR_MODE_BLOCK) segments.push(hintSegment())
  if (mode === BAR_MODE_DONE) segments.push(doneHintSegment())
  segments.push(segment('S5a', 'right', 7, [run('tok ' + formatCount(bucketTotal(featureTotalBucket(ledger))), { dimColor: true })]))
  segments.push(segment('S5b', 'right', 3, [run('· 会话 ' + formatCount(sessionTotal(ledger)), { dimColor: true })]))
  if (typeof cost === 'number') segments.push(segment('S6', 'right', 2, [run('· ' + formatUsd(cost), { dimColor: true })]))
  return segments
}

function withoutSegment(segments, id) {
  return segments.filter((item) => item.id !== id)
}

function replaceSegment(segments, id, rebuild) {
  return segments.map((item) => (item.id === id ? rebuild(item) : item))
}

// narrowProgress：按 barWidth 载荷重建 S3（6 格保留「任务 a/b」，0 格连同任务文本去掉）。
function narrowProgress(segments, barWidth) {
  return replaceSegment(segments, 'S3', (item) =>
    item.total ? progressSegment(item.closed, item.total, item.mode, item.count, item.tasksText, barWidth) : item,
  )
}

// 步骤名截到 budget 列（以 … 结尾），至少保留 1 个字。
function shrinkName(name, budget) {
  if (displayWidth(name) <= budget) return name
  const chars = Array.from(name)
  if (chars.length <= 1) return name
  const minimal = chars[0] + ELLIPSIS
  return budget <= displayWidth(minimal) ? minimal : truncateToWidth(name, budget)
}

// S4 步骤名逐字截短：重建「胶囊 id 名称…」，名称至少保留 1 个字。
function shrinkStepSegment(segments, columns) {
  const stepSegment = segments.find((item) => item.id === 'S4')
  if (!stepSegment || !stepSegment.step || !stepSegment.step.name) return segments
  const step = stepSegment.step
  const excess = segmentsWidth(segments) - columns
  const name = shrinkName(step.name, displayWidth(step.name) - excess)
  return replaceSegment(segments, 'S4', () => bandStepSegment(stepSegment.mode, step, name))
}

// BandRenderer.fit：固定步骤表 S4h / S4d → S6 → S5b → S3 进度条 12→6 格 → S2 → S3 去进度条与「任务 a/b」→ S5a →
// S4 步骤名截短，每步重算，直到总宽 ≤ columns；仍超宽时交给 Text 的 truncate-end，不折行。
function fitSegments(segments, columns) {
  const steps = [
    (list) => withoutSegment(withoutSegment(list, 'S4h'), 'S4d'),
    (list) => withoutSegment(list, 'S6'),
    (list) => withoutSegment(list, 'S5b'),
    (list) => narrowProgress(list, BAND_BAR_NARROW_WIDTH),
    (list) => withoutSegment(list, 'S2'),
    (list) => narrowProgress(list, 0),
    (list) => withoutSegment(list, 'S5a'),
    (list) => shrinkStepSegment(list, columns),
  ]
  let current = segments
  for (const step of steps) {
    if (segmentsWidth(current) <= columns) return current
    current = step(current)
  }
  return current
}

// BandRenderer.buildMultiSegments：` ◆ DEVKIT `、`N 个进行中`、各 feature「名称 当前步骤 id」、`→ /devkit`，
// 右组本会话 token；超宽时先去右组，再从名单末尾逐项折叠为「等 N 个」（至少保留 1 项）。
function buildMultiSegments(snap, ledger, columns) {
  const items = snap.features
  const build = (keep, withRight) => {
    const runs = []
    items.slice(0, keep).forEach((item, index) => {
      if (index > 0) runs.push(run(MULTI_ITEM_GAP, { dimColor: true }))
      runs.push(run(item.feature))
      // snap.features 只含 current_step≠null 的 feature（D-24），步骤全关者不进进度行。
      const step = item.currentStep
      if (step.id) runs.push(run(' ' + step.id, { color: 'cyan' }))
    })
    if (keep < items.length) runs.push(run('  等 ' + (items.length - keep) + ' 个', { dimColor: true }))
    const segments = [
      badgeSegment(),
      segment('M1', 'left', 0, [run(items.length + ' 个进行中', { bold: true })]),
      segment('M2', 'left', 0, runs),
      hintSegment(),
    ]
    if (withRight) segments.push(segment('S5b', 'right', 3, [run('· 会话 ' + formatCount(sessionTotal(ledger)), { dimColor: true })]))
    return segments
  }
  let keep = items.length
  let segments = build(keep, true)
  if (segmentsWidth(segments) <= columns) return segments
  segments = build(keep, false)
  while (keep > 1 && segmentsWidth(segments) > columns) {
    keep -= 1
    segments = build(keep, false)
  }
  return segments
}

function bandSegmentsFor(snap, ledger, cost, columns) {
  if (snap.kind === KIND_MULTI) return buildMultiSegments(snap, ledger, columns)
  return fitSegments(buildSegments(snap, ledger, cost), columns)
}

function groupsOf(segments) {
  return {
    left: segments.filter((item) => item.group !== 'right'),
    right: segments.filter((item) => item.group === 'right'),
  }
}

// 左右组之间的空白：右组靠右，至少 2 列；无右组时不填充。
function fillerWidth(left, right, columns) {
  return right.length ? Math.max(displayWidth(SEGMENT_GAP), columns - segmentsWidth(left) - segmentsWidth(right)) : 0
}

// BandRenderer.plainText：左组 + 空白 + 右组的纯文本；Raster 进度条以静态帧字形表示（测试与排查用）。
function bandPlainText(segments, columns) {
  const { left, right } = groupsOf(segments)
  const join = (list) => list.map((item) => item.text).join(SEGMENT_GAP)
  return join(left) + (right.length ? ' '.repeat(fillerWidth(left, right, columns)) + join(right) : '')
}

// BandRenderer.render 的宽度：AbovePrompt bodyColumns（面板停靠时小于终端宽）→ viewport.columns → 120。
function columnsOf(e) {
  const fromProps = e && isPlainObject(e.props) ? e.props.bodyColumns : undefined
  if (typeof fromProps === 'number' && Number.isFinite(fromProps) && fromProps > 0) return Math.floor(fromProps)
  const fromViewport = e && e.viewport ? e.viewport.columns : undefined
  return typeof fromViewport === 'number' && Number.isFinite(fromViewport) && fromViewport > 0 ? Math.floor(fromViewport) : DEFAULT_COLUMNS
}

// 降级第 2 级：非 terminal surface（resolve 出的 Raster 只画空片段）或 BAR_RENDER = 'text' 时用 Text 进度条。
function rasterAvailable(ui, e) {
  return BAR_RENDER !== 'text' && Boolean(e) && e.surface === 'terminal' && typeof ui.Raster === 'function'
}

function waitKeyOf(snap, mode) {
  return mode === BAR_MODE_WAIT && snap.current ? snap.workspace + '::' + snap.feature + '::' + snap.current.id : ''
}

// BarModel（band 站点）：每次 render 重建，不保存跨 render 状态。
function bandBarModel(e, snap, width, settings, palette) {
  const mode = barModeOf(snap.current)
  return {
    requestId: String(e.requestId ?? ''),
    columns: width,
    closed: snap.closed,
    total: snap.total,
    mode,
    waitKey: waitKeyOf(snap, mode),
    working: e.props.isWorking === true,
    animate: settings.animationEnabled && isPaletteKnown(palette) && BAR_RENDER === 'raster' && snap.bandEnabled,
  }
}

function textElement(ui, text, style) {
  return ui.Text({ ...(style || {}), wrap: 'truncate-end', children: [text] })
}

// 一段的绘制元素：S3 的进度条在 Raster 可用时为 Raster（初始 cells 由 attach 给出），否则为 Text 进度条。
function segmentElements(ui, item, bar) {
  return item.runs.map((part) => (part.bar ? bar(item) : textElement(ui, part.text, part.style)))
}

// BandRenderer.render（同步，不调用宿主 API；只经 attach / detach 改动站点状态）：一行左右两组的 Box。
function renderBand(ui, e, snap, mod, frame) {
  const columns = columnsOf(e)
  const segments = bandSegmentsFor(snap, mod.ledger, mod.refresher.costUsd, columns)
  const useRaster = rasterAvailable(ui, e)
  let attached = false
  const bar = (item) => {
    if (!useRaster) return textElement(ui, textBar(item.closed, item.total, item.barWidth))
    attached = true
    const model = bandBarModel(e, snap, item.barWidth, mod.settings, themePalette(mod.theme))
    return ui.Raster({ key: BAR_KEY, columns: item.barWidth, rows: 1, cells: attachSite(mod.animator, SITE_BAND, model.requestId, model, frame) })
  }
  const { left, right } = groupsOf(segments)
  const children = []
  const pushGroup = (group) =>
    group.forEach((item, index) => {
      if (index > 0) children.push(textElement(ui, SEGMENT_GAP))
      children.push(...segmentElements(ui, item, bar))
    })
  pushGroup(left)
  if (right.length) {
    children.push(textElement(ui, ' '.repeat(fillerWidth(left, right, columns))))
    pushGroup(right)
  }
  // 进度条被丢段去掉、Text 降级或多 feature 行：撤下进度行站点。
  if (!attached) detachSite(mod.animator, SITE_BAND)
  return ui.Box({ flexDirection: 'row', children })
}

// ---------------------------------------------------------------------------
// PaneState：/devkit 面板的冻结快照（J2、§3.5 T14、N8-W6）
// ---------------------------------------------------------------------------

function createPaneState() {
  return {
    snap: createSnapshot(KIND_LOADING),
    featureTotal: emptyBucket(),
    sessionBuckets: { main: emptyBucket(), sub: emptyBucket() },
    recentTurns: [],
    cost: null,
    takenAt: null,
    // 当前标签（1 流水线 / 2 任务 / 3 Token），本会话内保留，下次打开沿用；名单下钻的详情页共用。
    tab: PANE_TAB_PIPELINE,
    // 名单下钻（D-25）：snap 为 multi 时按数字键读到的单 feature 详情 { snap, featureTotal, recentTurns, takenAt }，
    // null 为名单视图。属于冻结快照：只由按键、返回与按 r 改变，freeze 时清空。
    drill: null,
    // 最近一次下钻读取失败的 feature 名（名单顶部提示），空串为无。
    drillError: '',
    // 下钻请求序号：每次下钻、返回、按 r 与 /devkit 打开都加 1，过期的读取结果丢弃。
    drillSeq: 0,
  }
}

// PaneState.freeze：按值拷贝台账视图（每个桶 { ...bucket }、每轮记录逐条拷贝成新数组）；Snapshot 每次刷新整体替换，
// 直接引用。record 原地累加桶与追加记录不会改变已冻结的面板。不改 tab。
function freezePane(pane, snap, ledger, cost, now) {
  pane.snap = snap
  pane.featureTotal = { ...featureTotalBucket(ledger) }
  const buckets = sessionBuckets(ledger)
  pane.sessionBuckets = { main: { ...buckets.main }, sub: { ...buckets.sub } }
  pane.recentTurns = recentTurns(ledger).map(copyTurnRecord)
  pane.cost = typeof cost === 'number' ? cost : null
  pane.takenAt = now
  pane.drill = null
  pane.drillError = ''
}

// PaneState.view：面板当前视图。名单下钻时用详情的 snap / feature 累计 / 每轮记录 / 快照时间，其余（标签、本会话
// 分桶、费用）沿用冻结值；否则就是 pane 本身。
function paneView(pane) {
  if (pane.snap.kind !== KIND_MULTI || pane.drill === null) return pane
  return { ...pane, ...pane.drill }
}

// PaneState.isDrilled
function isPaneDrilled(pane) {
  return paneView(pane) !== pane
}

// 任务页可用：tasksSource=task.md 且 tasksTotal > 0。
function tasksPageAvailable(snap) {
  return snap.tasksSource === TASKS_SOURCE_TASK_MD && snap.tasksTotal > 0
}

// PaneState.runningTaskId：逐条任务中表格顺序第一条「执行中」的 id，无则空串。
function runningTaskId(pane) {
  const snap = pane.snap
  if (!tasksPageAvailable(snap) || !snap.itemsAvailable) return ''
  const item = snap.taskItems.find((task) => isRunningTask(task.state))
  return item ? item.id : ''
}

function taskKeyOf(id) {
  return 'task:' + id
}

// 滚动目标：当前标签为「任务」且有执行中任务时为 { in: 面板 id, to: { key: 'task:<id>' } }，否则 null（按当前视图）。
function paneScrollTargetOf(pane) {
  if (pane.tab !== PANE_TAB_TASKS) return null
  const id = runningTaskId(paneView(pane))
  return id ? { in: PANE_ID, to: { key: taskKeyOf(id) } } : null
}

// ---------------------------------------------------------------------------
// PaneRenderer：/devkit 面板（§4.2.6；同步，不调用宿主 API，只读 PaneState，只经 attach / detach 改动站点状态）
// ---------------------------------------------------------------------------

function line(ui, text, style) {
  return ui.Text({ ...(style || {}), children: [text] })
}

function rowOf(ui, children, key) {
  return ui.Box({ ...(key ? { key } : {}), flexDirection: 'row', children })
}

function blankLine(ui) {
  return line(ui, ' ')
}

function ruleLine(ui, columns) {
  return line(ui, PANE_RULE.repeat(Math.max(1, columns)), { dimColor: true })
}

function hexOf(color) {
  return '#' + (color & 0xffffff).toString(16).padStart(6, '0')
}

function twoDigits(value) {
  return String(value).padStart(2, '0')
}

// 快照时间：PaneState.takenAt 的本地时间 HH:MM:SS。
function clockText(ms) {
  const date = new Date(typeof ms === 'number' && Number.isFinite(ms) ? ms : 0)
  return twoDigits(date.getHours()) + ':' + twoDigits(date.getMinutes()) + ':' + twoDigits(date.getSeconds())
}

function switchOffLine(ui, snap) {
  return line(ui, '常驻进度行已关闭（source=' + (snap.switchSource || '-') + '）' + SWITCH_OFF_HINT, { color: 'yellow' })
}

// 分列行「任务部分」（§4.2.6 表）：a/b、0/0 或带括号说明的 -。
function tasksPartOf(snap) {
  if (snap.tasksSource === TASKS_SOURCE_TASK_MD) return snap.tasksTotal > 0 ? snap.tasksClosed + '/' + snap.tasksTotal : TASKS_PART_EMPTY
  if (snap.tasksSource === TASKS_SOURCE_MISSING) return TASKS_PART_NO_TASK_MD
  if (snap.tasksSource === TASKS_SOURCE_INVALID) return TASKS_PART_INVALID
  return TASKS_PART_UNAVAILABLE
}

// BarModel（pane 站点）：columns = min(36, bodyColumns − 14)；working 取 BarAnimator.lastWorking()。
function paneBarModel(snap, columns, mod) {
  const mode = barModeOf(snap.current)
  return {
    requestId: PANE_ID,
    columns,
    closed: snap.closed,
    total: snap.total,
    mode,
    waitKey: waitKeyOf(snap, mode),
    working: lastWorking(mod.animator),
    animate: mod.settings.animationEnabled && isPaletteKnown(themePalette(mod.theme)) && BAR_RENDER === 'raster' && snap.bandEnabled,
  }
}

// 静态 Raster（阶段小进度条、分段条、比例条、sparkline）：Raster 可用时画格子，否则退为同宽 Text。
function staticRaster(ui, e, key, cells) {
  if (rasterAvailable(ui, e)) return ui.Raster({ key, columns: cells.length, rows: 1, cells: encodeCells(cells) })
  return line(ui, cells.map((item) => String.fromCodePoint(item[0])).join(''))
}

// 头部合并进度条（动效站点 pane）：小于 6 格不画；Raster 不可用时为 Text 进度条。
function paneHeaderBar(ui, e, mod, snap, columns, frame) {
  const width = Math.min(PANE_BAR_MAX_COLUMNS, columns - PANE_BAR_MARGIN)
  if (width < MIN_BAR_COLUMNS || !rasterAvailable(ui, e)) {
    detachSite(mod.animator, SITE_PANE)
    return width < MIN_BAR_COLUMNS ? null : line(ui, textBar(snap.closed, snap.total, width) || GLYPH_EMPTY.repeat(width))
  }
  const model = paneBarModel(snap, width, mod)
  return ui.Raster({ key: BAR_KEY, columns: width, rows: 1, cells: attachSite(mod.animator, SITE_PANE, PANE_ID, model, frame) })
}

// PaneRenderer.renderHeader：开关提示、徽章 + feature、元信息、合并进度条、分列行、胶囊行、快照时间、标签、分隔线。
// pane 为当前视图（paneView）；onBack 非 null 时（名单下钻详情）标签行末尾追加「b: 返回」。
function renderPaneHeader(ui, e, mod, pane, columns, frame, handlers) {
  const { onRefresh, onTab, onBack } = handlers
  const snap = pane.snap
  const summary = snap.summary
  const rows = []
  if (!snap.bandEnabled) rows.push(switchOffLine(ui, snap))
  rows.push(rowOf(ui, [line(ui, BADGE_TEXT, BADGE_STYLE), line(ui, '  '), line(ui, snap.feature, { bold: true })]))
  const meta = [scaleModeText(summary), textOf(summary.branch), textOf(summary.state)].filter(Boolean)
  rows.push(line(ui, meta.join(PANE_SEPARATOR), { dimColor: true, wrap: 'truncate-end' }))
  rows.push(blankLine(ui))
  const bar = paneHeaderBar(ui, e, mod, snap, columns, frame)
  const counts = [line(ui, '  ' + snap.closed + '/' + snap.total, { bold: true }), line(ui, '  ' + percentOf(snap.closed, snap.total) + '%', { bold: true })]
  rows.push(rowOf(ui, bar ? [bar, ...counts] : counts))
  rows.push(line(ui, '步骤 ' + snap.stepsClosed + '/' + snap.stepsTotal + PANE_SEPARATOR + '任务 ' + tasksPartOf(snap)))
  const mode = barModeOf(snap.current)
  const capsule = CAPSULES[mode]
  const stepRuns = [line(ui, ' ' + capsule.icon + ' ' + capsule.label + ' ', { backgroundColor: capsule.backgroundColor, color: capsule.color, bold: true })]
  if (snap.current) {
    stepRuns.push(line(ui, ' ' + snap.current.id, { bold: true }))
    if (snap.current.name) stepRuns.push(line(ui, ' ' + snap.current.name))
  } else {
    stepRuns.push(line(ui, '  ' + DONE_HINT_TEXT, { dimColor: true }))
  }
  rows.push(rowOf(ui, stepRuns))
  rows.push(line(ui, '快照 ' + clockText(pane.takenAt) + PANE_SNAPSHOT_HINT, { dimColor: true }))
  rows.push(blankLine(ui))
  const tabs = []
  PANE_TABS.forEach((item, index) => {
    const current = pane.tab === item.tab
    if (index > 0) tabs.push(line(ui, '   '))
    // plain Button 由宿主绘制「hotkey: label」，label 不得再拼 hotkey；当前页标记放在按钮之前（「▸ 1: 流水线」）。
    if (current) tabs.push(line(ui, PANE_TAB_MARK, { color: 'cyan', bold: true }))
    tabs.push(
      ui.Button({
        key: 'tab:' + item.hotkey,
        label: item.label,
        hotkey: item.hotkey,
        plain: true,
        ...(current ? {} : { dimColor: true }),
        onPress: () => onTab(item.tab),
      }),
    )
  })
  tabs.push(line(ui, '      '))
  tabs.push(ui.Button({ key: 'refresh', label: '刷新', hotkey: 'r', plain: true, dimColor: true, onPress: onRefresh }))
  if (onBack) {
    tabs.push(line(ui, '   '))
    tabs.push(ui.Button({ key: 'back', label: DRILL_BACK_LABEL, hotkey: DRILL_BACK_HOTKEY, plain: true, dimColor: true, onPress: onBack }))
  }
  rows.push(rowOf(ui, tabs))
  rows.push(ruleLine(ui, columns))
  return rows
}

// 流水线页阶段：`S<数字>` 的首个数字在阶段名表中时为该数字，否则为空串（「其他」）。
function phaseKeyOf(id) {
  const match = /^S(\d)/.exec(id)
  return match && Object.prototype.hasOwnProperty.call(PHASE_NAMES, match[1]) ? match[1] : ''
}

// 按阶段号把相邻步骤分组（pipeline 顺序）。
function phaseGroupsOf(pipeline) {
  const groups = []
  for (const step of pipeline) {
    const key = phaseKeyOf(step.id)
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.steps.push(step)
    else groups.push({ key, steps: [step] })
  }
  return groups
}

function padText(ui, text, columns) {
  return line(ui, ' '.repeat(Math.max(0, columns - displayWidth(text))))
}

// PaneRenderer.renderPipelineTab（P3 分组）：含当前断点的组逐步展开，其余组只有组头。
function renderPipelineTab(ui, e, pane, columns) {
  const snap = pane.snap
  const current = snap.current
  const palette = createPalette(PALETTE_UNKNOWN)
  const rows = []
  if (!current) rows.push(line(ui, ALL_CLOSED_TEXT, { color: 'green' }))
  const usedKeys = new Set()
  for (const group of phaseGroupsOf(snap.pipeline)) {
    const name = group.key ? PHASE_NAMES[group.key] : PHASE_OTHER
    const total = group.steps.length
    const closed = group.steps.filter((step) => isClosedState(step.state)).length
    const hasCurrent = Boolean(current) && group.steps.some((step) => step.id === current.id)
    const nameStyle = closed === total ? { color: 'green' } : hasCurrent ? { bold: true } : { dimColor: true }
    let key = 'phase:' + (group.key || 'other')
    for (let n = 2; usedKeys.has(key); n += 1) key = 'phase:' + (group.key || 'other') + ':' + n
    usedKeys.add(key)
    const head = [
      line(ui, name, nameStyle),
      padText(ui, name, PHASE_NAME_COLUMNS),
      staticRaster(ui, e, key, staticGradientCells(closed, total, PHASE_BAR_COLUMNS, palette)),
      line(ui, ' ' + closed + '/' + total, { dimColor: true }),
    ]
    if (closed === total) head.push(line(ui, PHASE_DONE_TEXT, { dimColor: true }))
    else if (!hasCurrent && closed === 0) head.push(line(ui, '  ' + total + ' 个步骤未开始', { dimColor: true }))
    else if (!hasCurrent) head.push(line(ui, '  ' + (total - closed) + ' 个步骤未关闭', { dimColor: true }))
    rows.push(rowOf(ui, head))
    if (!hasCurrent) continue
    for (const step of group.steps) {
      const isCurrent = step.id === current.id
      const stepColor = colorOfState(step.state)
      const runs = [
        line(ui, isCurrent ? '  ➜ ' : '    ', isCurrent ? { color: 'yellow', bold: true } : {}),
        line(ui, iconOfState(step.state), stepColor ? { color: stepColor } : {}),
        line(ui, ' ' + step.id, isCurrent ? { bold: true } : { dimColor: true }),
      ]
      if (step.name) runs.push(line(ui, ' ' + step.name, isCurrent ? { bold: true } : {}))
      if (isCurrent && step.note) {
        const used = 4 + displayWidth(iconOfState(step.state)) + 1 + displayWidth(step.id) + (step.name ? 1 + displayWidth(step.name) : 0)
        const room = columns - used - 2
        if (room > 6) runs.push(line(ui, '  ' + truncateToWidth(step.note, room), { dimColor: true }))
      }
      rows.push(rowOf(ui, runs))
    }
  }
  return rows
}

// 任务页一条任务：前缀 + 图标 + id + 描述（按剩余宽度截断）+ 右对齐状态；key 为 task:<id>。
function taskRow(ui, item, columns, isFirstRunning, key) {
  const style = taskStyleOf(item.state)
  const running = isRunningTask(item.state)
  const failed = isFailedTask(item.state)
  const prefix = isFirstRunning ? PANE_TAB_MARK : '  '
  const stateText = '  ' + item.state
  const left = displayWidth(prefix) + displayWidth(style.icon) + 1 + displayWidth(item.id) + 1
  const room = Math.max(0, columns - left - displayWidth(stateText))
  const desc = truncateToWidth(item.desc, room)
  const idStyle = isFirstRunning ? { color: 'cyan', bold: true } : failed ? { color: 'red', bold: true } : { dimColor: true }
  const descStyle = running ? { bold: true } : item.state === TASK_STATE_DONE ? { dimColor: true } : {}
  return rowOf(
    ui,
    [
      line(ui, prefix, isFirstRunning ? { color: 'cyan', bold: true } : {}),
      line(ui, style.icon, style.style),
      line(ui, ' ' + item.id, idStyle),
      line(ui, ' ' + desc, descStyle),
      line(ui, ' '.repeat(Math.max(0, room - displayWidth(desc)))),
      line(ui, stateText, running || failed ? { ...style.style, bold: true } : style.style),
    ],
    key,
  )
}

// PaneRenderer.renderTasksTab（R16）：摘要、分段条、图例、分隔线、逐条任务；不可用时只有任务部分说明。
function renderTasksTab(ui, e, pane, columns) {
  const snap = pane.snap
  if (!tasksPageAvailable(snap)) return [line(ui, '任务 ' + tasksPartOf(snap))]
  const rows = []
  const runningId = runningTaskId(pane)
  const failedCount = snap.byState[TASK_STATE_FAILED] || 0
  const summary = [line(ui, '已关闭 ', { dimColor: true }), line(ui, snap.tasksClosed + '/' + snap.tasksTotal, { bold: true })]
  if (runningId) summary.push(line(ui, '   执行中 ', { dimColor: true }), line(ui, runningId, { color: 'cyan', bold: true }))
  if (failedCount > 0) summary.push(line(ui, '   失败 ', { dimColor: true }), line(ui, String(failedCount), { color: 'red', bold: true }))
  rows.push(rowOf(ui, summary))
  const inline = !(isPlainObject(e.props) && e.props.placement === 'dock')
  const barColumns = inline ? Math.min(TASKS_BAR_INLINE_MAX_COLUMNS, columns - 2) : columns - 2
  if (barColumns >= MIN_BAR_COLUMNS) rows.push(staticRaster(ui, e, 'tasks-bar', segmentCells(snap.byState, snap.tasksTotal, barColumns)))
  const legend = []
  Object.entries(snap.byState).forEach(([state, count], index) => {
    if (index > 0) legend.push(line(ui, '   '))
    legend.push(line(ui, '■ ', { color: hexOf(taskSegmentColorOf(state)) }), line(ui, state + ' ', { dimColor: true }), line(ui, String(count)))
  })
  if (legend.length) rows.push(rowOf(ui, legend))
  rows.push(ruleLine(ui, columns))
  if (!snap.itemsAvailable) {
    rows.push(line(ui, TASKS_UPGRADE_TEXT, { dimColor: true }))
    return rows
  }
  const usedKeys = new Set()
  for (const item of snap.taskItems) {
    let key = taskKeyOf(item.id)
    for (let n = 2; usedKeys.has(key); n += 1) key = taskKeyOf(item.id) + '#' + n
    usedKeys.add(key)
    rows.push(taskRow(ui, item, columns, runningId !== '' && key === taskKeyOf(runningId), key))
  }
  if (snap.itemsTruncated) rows.push(line(ui, ELLIPSIS + ' 另有 ' + Math.max(0, snap.tasksTotal - snap.taskItems.length) + ' 条未列出', { dimColor: true }))
  if (inline && snap.taskItems.length > paneBodyRows(e)) rows.push(line(ui, '共 ' + snap.taskItems.length + ' 条', { dimColor: true }))
  return rows
}

// Pane.scroll.bodyRows（可见行数）；缺失时按无穷（不加「共 n 条」）。
function paneBodyRows(e) {
  const scroll = isPlainObject(e.props) ? e.props.scroll : undefined
  const rows = isPlainObject(scroll) ? scroll.bodyRows : undefined
  return typeof rows === 'number' && Number.isFinite(rows) && rows >= 0 ? rows : Infinity
}

// 本会话合计（与 TokenLedger.sessionTotal 同口径，读冻结的分桶）。
function frozenSessionTotal(pane) {
  return bucketTotal(pane.sessionBuckets.main) + (INCLUDE_SUBAGENT_TURNS ? bucketTotal(pane.sessionBuckets.sub) : 0)
}

// PaneRenderer.renderTokenTab（R17）：feature 累计与分项、每轮走势、本会话与 main / sub 比例条、统计范围说明。
function renderTokenTab(ui, e, mod, pane, columns) {
  const palette = themePalette(mod.theme)
  const feature = pane.featureTotal
  const rows = []
  rows.push(
    rowOf(ui, [
      line(ui, 'feature 累计  ', { dimColor: true }),
      line(ui, formatCount(bucketTotal(feature)), { color: 'cyan', bold: true }),
      line(ui, '   tokens · ' + feature.turns + ' 轮', { dimColor: true }),
    ]),
  )
  rows.push(
    line(
      ui,
      '输入 ' + formatCount(feature.input) + '  输出 ' + formatCount(feature.output) + '  缓存读 ' + formatCount(feature.cacheRead) + '  缓存写 ' + formatCount(feature.cacheWrite),
    ),
  )
  rows.push(blankLine(ui))
  const sparkColumns = Math.min(SPARK_MAX_COLUMNS, columns - 2)
  rows.push(line(ui, '每轮用量（feature 最近 ' + sparkColumns + ' 轮，输入+输出+缓存读写）', { dimColor: true }))
  const turns = sparkTurnsOf(pane.recentTurns, INCLUDE_SUBAGENT_TURNS, sparkColumns)
  if (turns.length === 0) {
    rows.push(line(ui, SPARK_EMPTY_TEXT, { dimColor: true }))
  } else {
    rows.push(rowOf(ui, [line(ui, '  '), staticRaster(ui, e, 'spark', sparkCells(turns, sparkColumns, palette))]))
    let peak = turns[0].n
    let peakIndex = 0
    let sum = 0
    turns.forEach((turn, index) => {
      sum += turn.n
      if (turn.n > peak) {
        peak = turn.n
        peakIndex = index
      }
    })
    rows.push(
      rowOf(ui, [
        line(ui, '  峰值 ', { dimColor: true }),
        line(ui, formatCount(peak)),
        line(ui, '（倒数第 ' + (turns.length - peakIndex) + ' 轮）', { dimColor: true }),
        line(ui, ' · 均值 ', { dimColor: true }),
        line(ui, formatCount(Math.round(sum / turns.length))),
        line(ui, '  '),
        line(ui, '▇', { color: hexOf(palette.stops[0]) }),
        line(ui, ' main ', { dimColor: true }),
        line(ui, '▇', { color: hexOf(palette.stops[2]) }),
        line(ui, ' sub', { dimColor: true }),
      ]),
    )
  }
  rows.push(blankLine(ui))
  const session = frozenSessionTotal(pane)
  const sessionRow = [line(ui, '本会话  ', { dimColor: true }), line(ui, formatCount(session), { color: 'cyan', bold: true })]
  if (pane.cost !== null) sessionRow.push(line(ui, '   费用 ', { dimColor: true }), line(ui, formatUsd(pane.cost), { bold: true }))
  rows.push(rowOf(ui, sessionRow))
  const inline = !(isPlainObject(e.props) && e.props.placement === 'dock')
  const barColumns = inline ? SESSION_BAR_INLINE_COLUMNS : SESSION_BAR_DOCK_COLUMNS
  for (const [name, label] of [['main', '  main '], ['sub', '  sub  ']]) {
    const bucket = pane.sessionBuckets[name]
    rows.push(
      rowOf(ui, [
        line(ui, label, { dimColor: true }),
        staticRaster(ui, e, 'session-' + name, staticGradientCells(bucketTotal(bucket), session, barColumns, palette)),
        line(ui, '  ' + formatCount(bucketTotal(bucket))),
        line(ui, '  ' + String(bucket.turns).padStart(2) + ' 轮', { dimColor: true }),
      ]),
    )
  }
  rows.push(blankLine(ui))
  rows.push(line(ui, SCOPE_NOTE_TEXT, { dimColor: true }))
  return rows
}

// 多 feature 面板一行的步骤部分（D-24）：「  图标 ID 步骤名」；used 为行首已占列数（前缀 + 名称），超宽只截断步骤名
// （放不下则省去），名称与 ID 不截断。
function multiStepText(item, used, columns) {
  const step = item.currentStep
  if (!step) return MULTI_ROW_GAP + ALL_CLOSED_TEXT
  const text = MULTI_ROW_GAP + iconOfState(step.state) + (step.id ? ' ' + step.id : '')
  if (!step.name) return text
  const name = truncateToWidth(step.name, columns - used - displayWidth(text) - 1)
  return name ? text + ' ' + name : text
}

// 名单一行（D-25）：前 9 行为 plain Button（key drill:<n>，宿主绘制「n: 」前缀，label 为 feature 名）+ 步骤部分 Text；
// 第 10 行起无热键，以「• 」开头。关闭组整行弱化。
function multiFeatureRow(ui, item, index, columns, onDrill) {
  const closed = item.currentStep === null
  const style = closed ? { dimColor: true } : {}
  if (index >= DRILL_MAX) {
    const head = MULTI_ROW_BULLET + item.feature
    return line(ui, head + multiStepText(item, displayWidth(head), columns), style)
  }
  const hotkey = String(index + 1)
  const used = displayWidth(hotkey + ': ' + item.feature)
  return rowOf(ui, [
    ui.Button({
      key: DRILL_KEY_PREFIX + hotkey,
      label: item.feature,
      hotkey,
      plain: true,
      ...(closed ? { dimColor: true } : {}),
      onPress: () => onDrill(item.feature),
    }),
    line(ui, multiStepText(item, used, columns), style),
  ])
}

// 名单页（D-24 分组 + D-25 下钻）：读取失败提示、进行中组、关闭组（连续编号）、超过 9 个时的说明。
function multiPaneRows(ui, pane, columns, onDrill) {
  const snap = pane.snap
  const rows = []
  if (pane.drillError) rows.push(line(ui, pane.drillError + DRILL_FAILED_SUFFIX, { color: 'yellow' }))
  if (snap.features.length > 0) {
    rows.push(line(ui, MULTI_ACTIVE_TITLE, { bold: true }))
    snap.features.forEach((item, index) => rows.push(multiFeatureRow(ui, item, index, columns, onDrill)))
  } else {
    rows.push(line(ui, MULTI_NONE_TEXT))
  }
  if (snap.closedFeatures.length > 0) {
    rows.push(blankLine(ui))
    rows.push(line(ui, MULTI_CLOSED_TITLE, { bold: true }))
    const offset = snap.features.length
    snap.closedFeatures.forEach((item, index) => rows.push(multiFeatureRow(ui, item, offset + index, columns, onDrill)))
  }
  if (snap.features.length + snap.closedFeatures.length > DRILL_MAX) rows.push(line(ui, DRILL_OVERFLOW_TEXT, { dimColor: true }))
  return rows
}

// 名单页底部：可下钻时「1–n 查看详情 · Esc 关闭」（n ≤ 9；只有 1 个时为「1 查看详情 · Esc 关闭」），否则「Esc 关闭」。
function multiFooterText(snap) {
  const count = Math.min(DRILL_MAX, snap.features.length + snap.closedFeatures.length)
  if (count === 0) return PANE_FOOTER_PLAIN_TEXT
  return (count === 1 ? '1' : '1–' + count) + DRILL_FOOTER_SUFFIX
}

// PaneRenderer.render(ui, e, mod, frame, handlers)：handlers = { onRefresh, onTab, onDrill, onBack }（Pane hook 内创建的闭包）。
// 当前视图为 active（单 feature，或名单下钻的详情）时为头部 + 当前标签页 + 底部快捷键行；multi 名单 / idle / absent
// 沿用 §4.2.3 文案，无标签、无进度条。只读 mod.pane（冻结快照）。
function renderPane(ui, e, mod, frame, handlers) {
  const pane = mod.pane
  const view = paneView(pane)
  const snap = view.snap
  const columns = columnsOf(e)
  const rows = []
  if (snap.kind !== KIND_ACTIVE) {
    detachSite(mod.animator, SITE_PANE)
    if (snap.kind === KIND_MULTI && !snap.bandEnabled) rows.push(switchOffLine(ui, snap))
    if (snap.kind === KIND_MULTI) rows.push(...multiPaneRows(ui, pane, columns, handlers.onDrill))
    else if (snap.kind === KIND_IDLE) rows.push(line(ui, IDLE_TEXT))
    else rows.push(line(ui, ABSENT_TEXT))
    rows.push(blankLine(ui))
    rows.push(ui.Button({ key: 'refresh', label: '刷新', hotkey: 'r', plain: true, onPress: handlers.onRefresh }))
    rows.push(line(ui, snap.kind === KIND_MULTI ? multiFooterText(snap) : PANE_FOOTER_PLAIN_TEXT, { dimColor: true }))
    return ui.Box({ flexDirection: 'column', children: rows })
  }
  const headerHandlers = { ...handlers, onBack: view === pane ? null : handlers.onBack }
  rows.push(...renderPaneHeader(ui, e, mod, view, columns, frame, headerHandlers))
  let footer = PANE_FOOTER_TEXT
  if (pane.tab === PANE_TAB_TASKS) {
    rows.push(...renderTasksTab(ui, e, view, columns))
    const inline = !(isPlainObject(e.props) && e.props.placement === 'dock')
    if (inline && tasksPageAvailable(snap) && snap.itemsAvailable && snap.taskItems.length > paneBodyRows(e)) footer = PANE_FOOTER_SCROLL_TEXT
  } else if (pane.tab === PANE_TAB_TOKENS) {
    rows.push(...renderTokenTab(ui, e, mod, view, columns))
  } else {
    rows.push(...renderPipelineTab(ui, e, view, columns))
  }
  rows.push(blankLine(ui))
  rows.push(line(ui, footer, { dimColor: true }))
  return ui.Box({ flexDirection: 'column', children: rows })
}

// ---------------------------------------------------------------------------
// CommandHandlers：/devkit、/devkit-refresh 与面板刷新、滚动
// ---------------------------------------------------------------------------

// 多 feature 刷新文本（D-24）：只列可计数的 feature；步骤已全部关闭的附在其后。
function multiRefreshText(snap) {
  const names = (items) => items.map((item) => item.feature).join(', ')
  const count = snap.features.length
  let text
  if (count > 1) text = '已刷新：多个进行中 feature（' + names(snap.features) + '）'
  else if (count === 1) text = '已刷新：1 个进行中 feature（' + names(snap.features) + '）'
  else text = '已刷新：没有进行中的步骤'
  if (snap.closedFeatures.length > 0) {
    text += '；' + snap.closedFeatures.length + ' 个步骤已全部关闭，可执行 flow complete（' + names(snap.closedFeatures) + '）'
  }
  return text
}

// CommandHandlers.refreshText：active 时为合并口径，有任务统计时附「（步骤 x/y · 任务 a/b）」，步骤全关时再附
// 「；步骤已全部关闭，可执行 flow complete」（D-25）；开关关闭再附说明。
function refreshText(snap) {
  let text
  if (snap.kind === KIND_ACTIVE) {
    text = '已刷新：' + snap.feature + ' ' + snap.closed + '/' + snap.total
    if (hasTasks(snap)) text += '（步骤 ' + snap.stepsClosed + '/' + snap.stepsTotal + ' · 任务 ' + snap.tasksClosed + '/' + snap.tasksTotal + '）'
    if (!snap.current) text += REFRESH_DONE_SUFFIX
  } else if (snap.kind === KIND_MULTI) text = multiRefreshText(snap)
  else if (snap.kind === KIND_IDLE) text = '已刷新：当前没有进行中的 DevKit feature'
  else text = '已刷新：未检测到 DevKit 流程'
  const flowing = snap.kind === KIND_ACTIVE || snap.kind === KIND_MULTI
  return flowing && !snap.bandEnabled ? text + SWITCH_OFF_NOTE : text
}

// CommandHandlers.scrollToRunningTask：任务页且有执行中任务时把该行滚入视野；deny 与异常一律忽略。
async function scrollToRunningTask($, mod) {
  const target = paneScrollTargetOf(mod.pane)
  if (target === null) return
  try {
    await $.ui.scroll(target)
  } catch {
    // 任务页新树尚未挂载或面板不可滚动时被拒，忽略。
  }
}

// CommandHandlers.onDevkit：刷新 → 撤下面板进度条站点（清掉上次打开残留的推进）→ 冻结（名单下钻回到名单）→ 打开
// （holdToasts）→ 滚动。
async function onDevkitCommand($, mod) {
  mod.pane.drillSeq += 1
  const snap = await refresh($, mod)
  const now = await nowMs($)
  detachSite(mod.animator, SITE_PANE)
  freezePane(mod.pane, snap, mod.ledger, mod.refresher.costUsd, now)
  try {
    await $.ui.open({ id: PANE_ID, title: 'DevKit', focus: true, closeOnEscape: true, holdToasts: true })
  } catch {
    // 面板无法放置时静默。
  }
  await scrollToRunningTask($, mod)
  return {}
}

// CommandHandlers.loadDrill（D-25）：名单下钻读取某个 feature 的详情——经 withProbe 串行执行 `status --feature`，
// 再经台账队列只读取该 feature 的累计与每轮记录。任一步失败返回 null。
async function loadDrill($, mod, workspace, name) {
  let snap = null
  try {
    snap = await withProbe(mod.probe, () => fetchFeature($, mod.probe, workspace, name))
  } catch {
    snap = null
  }
  if (snap === null) return null
  const view = await loadFeatureView($, mod.ledger, snap)
  if (view === null) return null
  return { snap, featureTotal: { ...view.total }, recentTurns: view.recent.map(copyTurnRecord), takenAt: await nowMs($) }
}

// 名单中是否还有该 feature（进行中组或关闭组）。
function listHasFeature(snap, name) {
  return snap.kind === KIND_MULTI && [...snap.features, ...snap.closedFeatures].some((item) => item.feature === name)
}

// CommandHandlers.onPaneRefresh（按 r）：刷新当前视图。刷新 →（名单下钻且该 feature 仍在新名单中时）重读详情 → 推进判定
// （当前视图合并比例增大时登记面板头部推进）→ 冻结并恢复下钻 → 重绘 → 滚动。推进登记、冻结与恢复下钻之间不 await，
// 冻结后的第一次 render 以新模型 attach 并取 f0（先画旧位置）。详情读取失败时回到名单并提示；该 feature 已不在名单中
// （已 flow complete，或名单已变为单 feature / 无流程）时按新快照显示。
async function onPaneRefresh($, mod) {
  const pane = mod.pane
  pane.drillSeq += 1
  const seq = pane.drillSeq
  const before = paneView(pane).snap
  const drilled = isPaneDrilled(pane) ? before.feature : ''
  const snap = await refresh($, mod)
  const stillListed = drilled !== '' && listHasFeature(snap, drilled)
  const drill = stillListed ? await loadDrill($, mod, snap.workspace, drilled) : null
  const now = await nowMs($)
  if (seq !== pane.drillSeq) return
  const after = drill === null ? snap : drill.snap
  const advance = diffSnapshots(before, after)
  if (advance.comparable && ratioIncreased(advance)) pushSite(mod.animator, SITE_PANE, advance.fromRatio, advance.toRatio)
  freezePane(pane, snap, mod.ledger, mod.refresher.costUsd, now)
  if (drill !== null) pane.drill = drill
  else if (stillListed) pane.drillError = drilled
  invalidateRender($)
  await scrollToRunningTask($, mod)
}

// CommandHandlers.onPaneDrill（名单按 1–9）：读取该 feature 详情后切到详情页（标签沿用），失败时名单顶部提示；
// 期间若有返回、按 r 或重新打开面板（序号变化）或名单已换，结果丢弃。不刷新名单、不影响进度行与记账。
async function onPaneDrill($, mod, name) {
  const pane = mod.pane
  pane.drillSeq += 1
  const seq = pane.drillSeq
  const list = pane.snap
  if (list.kind !== KIND_MULTI) return
  const drill = await loadDrill($, mod, list.workspace, name)
  if (seq !== pane.drillSeq || pane.snap !== list) return
  if (drill === null) {
    pane.drillError = name
  } else {
    detachSite(mod.animator, SITE_PANE)
    pane.drill = drill
    pane.drillError = ''
  }
  invalidateRender($)
  if (drill !== null) await scrollToRunningTask($, mod)
}

// CommandHandlers.onPaneBack（详情页按 b）：回到名单（作废在途的下钻读取），不刷新。
function onPaneBack($, mod) {
  const pane = mod.pane
  pane.drillSeq += 1
  pane.drill = null
  pane.drillError = ''
  invalidateRender($)
}

async function onDevkitRefreshCommand($, mod) {
  const snap = await refresh($, mod)
  return { text: refreshText(snap) }
}

// ---------------------------------------------------------------------------
// ModEntry
// ---------------------------------------------------------------------------

async function registerCommands($) {
  try {
    await $.command.register({ name: 'devkit', description: '打开 DevKit 流程进度面板', immediate: true })
  } catch {
    // 注册失败（如与内置命令冲突）不影响其余能力。
  }
  try {
    await $.command.register({ name: 'devkit-refresh', description: '立即刷新 DevKit 进度', immediate: true })
  } catch {
    // 同上。
  }
}

function startTicker($, mod) {
  if (mod.ticker !== null) {
    try {
      mod.ticker.cancel()
    } catch {
      // 旧计时器已失效。
    }
  }
  mod.ticker = $.clock.every(TICK_MS, () => onTick($, mod))
}

// session.start 处理：第一条语句同步入队会话恢复（在任何 await 之前，N9-W3）→ 注册命令 → 初始化帧时钟 epoch
// → 读主题色板 → 15s 定时刷新 → 立即刷新一次。插件每次加载与热重载（含切换动效开关）都会触发。
async function onSessionStart($, mod, e, next) {
  restoreSession($, mod.ledger)
  await registerCommands($)
  try {
    initEpoch(mod.animator, await $.clock.now())
  } catch {
    // 取不到时间时由 render hook 兜底初始化。
  }
  await loadTheme($, mod.theme)
  try {
    startTicker($, mod)
  } catch {
    // 无定时器时仍有 turn.complete 与命令触发刷新。
  }
  refresh($, mod)
  return next(e)
}

// AbovePrompt 处理：取帧号 → 在任何提前返回之前用 isWorking 更新工作状态（N8-I1）→ 问卷让位或不可见时撤下
// 进度行站点并原样返回 → 否则绘制「进度行 + 宿主内容」；构造好返回值之后、return 之前触发调度（不等待）。
async function onAbovePrompt($, mod, e, next) {
  const now = await nowMs($)
  const animator = mod.animator
  if (animator.epoch === null) initEpoch(animator, now)
  const hasProps = Boolean(e) && isPlainObject(e.props)
  setWorking(animator, hasProps && e.props.isWorking === true)
  const snap = mod.refresher.snap
  if (!hasProps || e.props.hasSurvey === true || !isSnapshotVisible(snap)) {
    detachSite(animator, SITE_BAND)
    schedule($, animator).catch(() => {})
    return next(e)
  }
  let ui = null
  let mine = null
  try {
    ui = $.ui.resolve(e)
    mine = renderBand(ui, e, snap, mod, currentFrame(animator, now))
  } catch {
    mine = null
  }
  if (mine === null) {
    detachSite(animator, SITE_BAND)
    schedule($, animator).catch(() => {})
    return next(e)
  }
  const theirs = await next(e)
  const result = ui.Box({ flexDirection: 'column', children: theirs ? [mine, theirs] : [mine] })
  schedule($, animator).catch(() => {})
  return result
}

// Pane 处理（requestId 为 devkit-progress 时）：取帧号 → 在本函数内创建按 r 刷新、切标签、名单下钻与返回的闭包（`$` 经本函数参数进入
// 闭包）→ PaneRenderer 只按 PaneState 绘制 → 构造好返回值之后、return 之前触发调度（不等待）。
async function onPane($, mod, e, next) {
  if (!e || e.requestId !== PANE_ID) return next(e)
  const now = await nowMs($)
  const animator = mod.animator
  if (animator.epoch === null) initEpoch(animator, now)
  const ui = $.ui.resolve(e)
  const onRefresh = () => {
    onPaneRefresh($, mod).catch(() => {})
  }
  const onTab = (tab) => {
    mod.pane.tab = tab
    invalidateRender($)
    if (tab === PANE_TAB_TASKS) scrollToRunningTask($, mod).catch(() => {})
  }
  const onDrill = (name) => {
    onPaneDrill($, mod, name).catch(() => {})
  }
  const onBack = () => {
    onPaneBack($, mod)
  }
  const result = renderPane(ui, e, mod, currentFrame(animator, now), { onRefresh, onTab, onDrill, onBack })
  schedule($, animator).catch(() => {})
  return result
}

// 测试入口（纯函数，不调用宿主 API）：Token 页走势的数据过滤。kit 中 INCLUDE_SUBAGENT_TURNS 是模块常量、无法切换，
// 用例经此入口覆盖常量为假的分支（scripts/claude_mod_tests/devkit-progress.test.ts T-42）。
export { sparkTurnsOf }

// register(on, options)：options 为插件 userConfig 的取值（J9-B），动效开关由 ModSettings 解析。
export function register(on, options) {
  const settings = settingsFromOptions(options)
  const theme = createThemeSource()
  const mod = {
    probe: createStatusProbe(),
    refresher: createRefresher(),
    ledger: createTokenLedger(),
    // 15s 刷新定时器句柄（与 BarAnimator.timer 帧唤醒句柄相互独立）。
    ticker: null,
    settings,
    theme,
    animator: createBarAnimator(theme),
    // /devkit 面板的冻结快照与当前标签。
    pane: createPaneState(),
  }

  on('session.start', async ($, e, next) => onSessionStart($, mod, e, next))

  on('turn.start', async ($, e, next) => onTurnStart($, mod, e, next))

  on('turn.complete', async ($, e, next) => onTurnComplete($, mod, e, next))

  on('command.run', { command: 'devkit' }, async ($) => onDevkitCommand($, mod))

  on('command.run', { command: 'devkit-refresh' }, async ($) => onDevkitRefreshCommand($, mod))

  on('config.set', { key: 'theme' }, async ($, e, next) => onThemeConfigSet($, mod.theme, e, next))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => onAbovePrompt($, mod, e, next))

  on('ui.render', { component: 'Spinner' }, async ($, e, next) => renderSpinner(e, next, mod.refresher.snap))

  on('ui.render', { component: 'Pane' }, async ($, e, next) => onPane($, mod, e, next))
}
