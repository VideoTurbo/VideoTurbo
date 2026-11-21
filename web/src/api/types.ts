// 主控接口返回的数据结构。字段与后端保持一致（snake_case）。

// ---- 列表状态（字幕生成与影片修复共用） ----
/** 后端对外只给这 6 个状态；具体在做哪一步看 step。 */
export type ListStatus = 'pending' | 'queued' | 'running' | 'paused' | 'failed' | 'completed'
export type ListView = 'all' | 'active' | 'failed' | 'pending' | 'completed'

// ---- 字幕任务 ----
/** 后端按任务当前状态给出的可用操作；界面只按这份列表显示按钮。 */
export type JobAction =
  | 'start' | 'skip' | 'pause' | 'resume' | 'cancel' | 'retry' | 'priority' | 'complete' | 'reprocess' | 'download'

export type AsrMode = 'whisperjav' | 'faster_whisper'

export interface WhisperjavPass {
  pass_number: number
  state: 'waiting' | 'running' | 'done'
  model: string
  step?: number | null
  stage?: string | null
  started_seconds?: number | null
  finished_seconds?: number | null
  completed_scenes?: number | null
  total_scenes?: number | null
  current_scene?: number | null
}

export interface WhisperjavProgress {
  stage?: string | null
  model?: string | null
  passes?: WhisperjavPass[]
  total_passes?: number
  completed_scenes?: number | null
  total_scenes?: number | null
  current_scene?: number | null
}

export interface AsrProgress extends WhisperjavProgress {
  mode: AsrMode
  elapsed_seconds: number
  percent?: number
  completed_chunks?: number
  total_chunks?: number
  eta_seconds?: number | null
}

export interface TranslationStats {
  provider_cue_counts?: Record<string, number>
  provider_percentages?: Record<string, number>
  upstream_cue_counts?: Record<string, number>
  upstream_percentages?: Record<string, number>
  upstream_failure_counts?: Record<string, number>
}

export interface TranslationProgress extends TranslationStats {
  progress_percent: number
  completed_cues: number
  total_cues: number
  eta_seconds: number | null
  inflight_requests: number
  max_concurrency: number
  average_request_seconds?: number
  request_count: number
  fallback_count?: number
  retry_count: number
  split_count: number
  prompt_tokens?: number
  completion_tokens?: number
  last_error?: string | null
}

export interface Job {
  id: string
  status: ListStatus
  /** 排队或处理中的具体步骤，如“语音识别”。 */
  step?: string | null
  actions: JobAction[]
  media_path: string
  media_size: number
  library_id?: string | null
  library_name?: string | null
  /** 任务创建时从媒体库写入的语言对。 */
  source_language: string
  target_language: string
  source_language_label: string
  target_language_label: string
  poster_url?: string | null
  priority: number
  updated_at: string
  last_error?: string | null
  lease_owner?: string | null
  /** 本地媒体库：影片旁的字幕文件；网盘媒体库：上传到网盘的位置。 */
  subtitle_path?: string | null
  asr_progress?: AsrProgress | null
  translation_progress?: TranslationProgress | null
  translation_stats?: TranslationStats | null
}

export interface JobFile {
  kind: string
  label: string
  path?: string | null
  exists: boolean
  size: number
}

export interface JobDetail extends Job {
  created_at: string
  asr_metadata?: {
    asr_engine?: AsrMode
    model?: string
    passes?: { model: string }[]
    ensemble?: boolean
    device?: string
  } | null
  files?: JobFile[]
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  pages: number
}

export interface JobPage extends Page<Job> {
  counts?: Partial<Record<ListView, number>>
}

export interface BatchResult {
  succeeded: string[]
  failed: string[]
}

// ---- Worker ----
export interface TransferProgress {
  stage: string
  detail?: string | null
  percent?: number | null
  total_bytes?: number | null
  transferred_bytes?: number
  bytes_per_second?: number
  eta_seconds?: number | null
  elapsed_seconds?: number
  fps?: number
}

export interface Worker {
  name: string
  /** Worker 运行的 VideoTurbo 版本。 */
  version: string
  status: string
  last_seen_at: string
  gpu_name?: string | null
  gpu_utilization?: number | null
  memory_free_mb?: number | null
  asr_device?: string | null
  device_type?: string | null
  current_job_id?: string | null
  task_kind?: string | null
  supported_asr_modes: AsrMode[]
  supports_whisperjav_ensemble?: boolean
  restore_models?: string[]
  restore_unavailable_reason?: string | null
  restore_progress?: TransferProgress | null
  restore_jobs?: { job_id: string; progress?: TransferProgress | null }[]
  asr_mode?: AsrMode
  asr_stage?: string | null
  asr_elapsed_seconds?: number
  asr_total_chunks?: number
  asr_completed_chunks?: number
  asr_eta_seconds?: number | null
  asr_whisperjav_progress?: WhisperjavProgress | null
}

// ---- 翻译节点 ----
export interface Upstream {
  name: string
  role?: 'primary' | 'fallback'
  configured: boolean
  enabled: boolean
  concurrency: number
  active_requests: number
  cooldown_seconds: number
  consecutive_failures: number
  failure_count: number
  failure_threshold: number
  last_error?: string | null
  last_probe_status?: string | null
  last_probe_seconds?: number | null
  last_probe_at?: string | null
  last_probe_error?: string | null
}

export type NodePool = 'translation' | 'fallback'
export type NodeProtocol = 'openai_responses' | 'openai_chat' | 'vertex_ai'
export type NodeAuth = 'api_key' | 'service_account'

export interface LlmNode {
  id: string
  name: string
  pool: NodePool
  protocol: NodeProtocol
  base_url: string | null
  resolved_base_url?: string
  auth: NodeAuth
  project: string | null
  location: string
  model: string
  concurrency: number
  reasoning_effort: string | null
  enabled: boolean
  key_configured: boolean
  key_masked?: string
  models?: string[]
  models_fetched_at?: string | null
}

export interface NodePayload {
  name: string
  pool: NodePool
  protocol: NodeProtocol
  auth: NodeAuth
  base_url: string | null
  project: string | null
  location: string
  api_key: string | null
  copy_key_from?: string | null
  model: string
  concurrency: number
  reasoning_effort: string | null
  enabled: boolean
}

// ---- 配置 ----
/** 媒体库的可修改项；来源和文件夹只在添加时选择。 */
export interface LibraryPayload {
  name: string
  enabled: boolean
  source_language: string
  target_language: string
}

export interface MediaLibrary extends LibraryPayload {
  id: string
  /** "local"：主程序容器内的文件夹；其余是网盘插件的 id。 */
  source: string
  path: string
  source_label: string
  /** 来源类型：本地文件夹，或网盘插件的名称。 */
  source_type: string
  /** 网盘插件的图标（data URI），本地为空。 */
  source_icon: string
  /** 来源暂时不能使用的原因（插件未安装、插件设置没填写等）。 */
  problem: string | null
  source_language_label: string
  target_language_label: string
  media_count: number
  scanned_at: string | null
}

export interface FolderItem {
  name: string
  path: string
  /** 已作为这个媒体库的文件夹。 */
  library: string | null
  /** 在这个媒体库的文件夹里面。 */
  inside: string | null
  /** 里面有这个媒体库的文件夹。 */
  contains: string | null
}

export interface FolderPage {
  /** null：本地有多个媒体目录，正在列出这些目录。 */
  path: string | null
  /** null：已在最上层；""：回到媒体目录列表。 */
  parent: string | null
  /** 面包屑的起点：本地是媒体目录，网盘是 /。 */
  root: string | null
  /** 当前文件夹本身的使用情况。 */
  current: FolderItem | null
  folders: FolderItem[]
}

/** 主控内置语言表中的一项；媒体库只能从这里选择语言。 */
export interface LanguageInfo {
  code: string
  label: string
  can_be_source: boolean
  can_be_target: boolean
}

export type ConfigSection = 'scan' | 'subtitles' | 'translation' | 'server'
export type ConfigValues = Record<string, unknown>

export interface AppConfig {
  llm: { nodes: LlmNode[] }
  settings: Record<ConfigSection, ConfigValues>
  deployment: { kind?: Deployment; port: number; worker_port: number; restore_backup_dir?: string | null }
  languages: LanguageInfo[]
}

export interface AsrSettings {
  mode: AsrMode
  whisperjav_ensemble: boolean
}
export interface UiSettings {
  show_posters: boolean
  notice_sound: boolean
}
export interface CloudSettings {
  idle_minutes: number
  backstop_minutes: number
  update_jasna: boolean
  jasna_license_set: boolean
}

// ---- 云平台 ----
export type ShutdownMode = 'always' | 'off'

export interface CloudInstance {
  id: string
  title?: string | null
  status: 'running' | 'stopped' | 'starting' | string
  status_text: string
  gpu_id?: string | null
  gpu_name?: string | null
  gpu_count: number
  region?: string | null
  started_at?: number | null
  machine?: string | null
}

export interface CloudInstanceGroup {
  provider: string
  label: string
  error?: string | null
  capabilities?: string[]
  launch_methods?: string[]
  instances: CloudInstance[]
}

export interface CloudBinding {
  provider: string
  provider_label: string
  instance_id: string
  instance_title?: string | null
  instance?: CloudInstance | null
  instance_error?: string | null
  shutdown_mode: ShutdownMode
  state?: string
  configured: boolean
  provider_missing?: boolean
  can_shutdown?: boolean
  last_error?: string | null
  tunnel_error?: string | null
  next_attempt_at?: string | null
  idle_since?: string | null
  idle_minutes: number
}

export type LaunchState =
  | 'grabbing' | 'booting' | 'deploying' | 'running' | 'deploy_failed' | 'nvenc_failed' | 'failed' | 'cancelled'

export interface Launch {
  id: string
  provider: string
  state: LaunchState
  gpu_label: string
  gpu_count: number
  target_label: string
  services: string[]
  /** NVENC 检查未通过后，用户选择只继续部署字幕识别。 */
  asr_only?: boolean
  message?: string | null
  /** 部署失败是因为云主机的 SSH 主机密钥和记录的不一致：可以让用户确认后信任新密钥。 */
  host_key_changed?: boolean
  attempts?: number
  instance_id?: string | null
  /** 任务对应的实例：抢到的实例；开启已有实例时，抢到前就是目标实例。新建实例抢到前为空。 */
  target_instance_id?: string | null
  machine?: string | null
  created_at: string
  grabbed_at?: string | null
  log?: string
}

export interface NoticeSummary {
  unread: number
  latest_id: number
}
export interface Notice {
  id: number
  level: 'info' | 'success' | 'warning' | 'error'
  title: string
  message: string
  created_at: string
  read_at?: string | null
}

export interface CloudStatus {
  providers_configured: boolean
  auto_enabled: boolean
  jasna_license_set?: boolean
  launch_providers: { id: string; label: string }[]
  bindings: Record<string, CloudBinding>
  launches: Launch[]
  notices?: NoticeSummary
}

/** 插件表单字段的控件：单行文本（secret 时为密码框）、多行文本、数字、下拉框、开关。 */
export type PluginFieldType = 'text' | 'textarea' | 'number' | 'select' | 'switch'
/** 字段的值：文本和下拉框是字符串，数字是 number，开关是 boolean；null 表示没填。 */
export type FieldValue = string | number | boolean | null

/** 插件声明的配置字段（每份配置的字段，或插件设置的字段）。 */
export interface PluginField {
  key: string
  label: string
  type: PluginFieldType
  help?: string
  /** 只有文本类字段可能是密钥：已保存时不回显，留空表示不修改。 */
  secret?: boolean
  required?: boolean
  placeholder?: string
  default?: FieldValue
  trim?: boolean
  options?: { value: string; label: string }[]
  min?: number | null
  max?: number | null
  integer?: boolean
  /** 地址类字段：修改后必须重新填写已保存的密钥。 */
  binds_secrets?: boolean
}

export interface CloudProviderType {
  id: string
  label: string
  homepage?: string
  icon?: string
  fields: PluginField[]
  default_model_dir?: string
  credential_mode?: string
}

export type PluginKind = 'cloud' | 'media_server' | 'media_source' | 'notifier'

/** 插件来源：内置、Python 包（entry point）、在控制台按地址安装、本地上传。后两种可以卸载；按地址安装的可以在线更新，本地上传的重新上传即更新。 */
export type PluginSource = 'builtin' | 'package' | 'url' | 'upload'

/** 插件类型及其显示名称。 */
export interface PluginKindInfo { id: PluginKind; label: string }
/** 插件文件中的一个插件类：一个插件文件可以同时提供多种类型。 */
export interface PluginMember { id: string; kind: PluginKind; kind_label: string; label: string }

/** 已安装的插件。 */
export interface InstalledPlugin {
  /** 插件 id：在控制台安装的是插件文件的 id，内置和 Python 包的是插件类的 id。 */
  id: string
  /** 插件提供的类型，一个插件可以有多种。 */
  kinds: PluginKindInfo[]
  members: PluginMember[]
  source: PluginSource
  label: string
  /** 插件声明的版本号；内置插件不提供。 */
  version?: string | null
  homepage?: string
  /** data:image/… URI；空表示插件没有提供图标。 */
  icon?: string
  /** 以下仅在控制台安装的插件有；本地上传的 url 为空，filename 是上传的文件名。 */
  url?: string
  filename?: string
  sha256?: string
  installed_at?: string | null
  updated_at?: string | null
  /** 主控启动时没有加载的原因（加载失败或安全模式）。 */
  error?: string | null
  /** 使用该插件的配置数量（媒体库来源插件是使用它的媒体库数量）。 */
  usage: number
  /** 插件声明了插件设置：卡片上显示设置按钮。 */
  has_settings?: boolean
  /** 还有必填的插件设置没填写。 */
  settings_missing?: boolean
}

/** 插件设置弹窗的内容。密钥字段只返回掩码。 */
export interface PluginSettingsInfo {
  id: string
  label: string
  homepage?: string
  icon?: string
  description?: string
  fields: PluginField[]
  values: Record<string, FieldValue>
  /** 可以直接测试（媒体库来源插件）；其他插件在各自的配置里测试。 */
  testable: boolean
}

/** 添加媒体库时可选的网盘插件。 */
export interface MediaSourceOption {
  id: string
  label: string
  description: string
  icon: string
  has_settings: boolean
  /** 不能使用的原因（必填设置没填写）。 */
  problem: string | null
}

export interface PluginList {
  /** 主控以 VIDEO_TURBO_SAFE_MODE=1 启动：不加载、不能安装或更新插件。 */
  safe_mode: boolean
  kinds: PluginKindInfo[]
  plugins: InstalledPlugin[]
}

/** 安装前的静态检查结果：插件信息静态读取（未执行），读不到的为空字符串。 */
export interface PluginPreview {
  /** 本地上传时 url 为空，filename 是文件名。 */
  url: string; filename?: string; sha256: string; size: number
  id: string; label: string; version: string; homepage: string; description?: string
  /** 静态读出的类型和插件类；读不到时为空。 */
  kinds: PluginKindInfo[]
  members: { id: string; kind: PluginKind; kind_label: string; label: string }[]
  /** data:image/… URI；空表示没有读到图标。 */
  icon: string
  /** 远程图标：主程序下载并检查后的地址和文件指纹，安装时一并核对；icon_error 是下载或检查失败的原因（不影响安装）。 */
  icon_url?: string; icon_sha256?: string; icon_error?: string
}
/** 检查更新的结果：新内容静态检查的信息，changed 表示与已安装的不同。 */
export interface PluginUpdateCheck extends PluginPreview { changed: boolean }
/** 安装、检查更新、更新的结果；version 是新内容声明的版本号（静态读取，未执行）。 */
export interface PluginResult { id: string; kinds?: PluginKindInfo[]; label?: string; version?: string; sha256: string; changed: boolean
  /** 仅本地上传：替换了已上传安装的同一插件。 */
  updated?: boolean
}

/** 媒体服务器、通知插件的类型（含每份配置的字段）。 */
export interface PluginType {
  id: string
  label: string
  homepage?: string
  icon?: string
  description?: string
  fields: PluginField[]
}

/** 有多份配置的插件类型，对应接口路径。 */
export type ConnectionKind = 'media-servers' | 'notifiers'
export type NoticeMinLevel = 'info' | 'warning' | 'error'

/** 媒体服务器或通知的一份配置。密钥字段只返回掩码。 */
export interface PluginConnection {
  id: string
  type: string
  type_label: string
  name: string
  enabled: boolean
  /** 插件未安装或加载失败时为 false，配置保留。 */
  available: boolean
  summary: string
  values: Record<string, FieldValue>
  min_level?: NoticeMinLevel
  last_ok_at?: string | null
  last_error?: string | null
  last_error_at?: string | null
}

export interface ConnectionPayload {
  type?: string
  name: string
  enabled: boolean
  values: Record<string, FieldValue>
  min_level?: NoticeMinLevel
}

/** 插件已卸载或加载失败的平台配置。 */
export interface CloudOrphanProfile {
  id: string
  label: string
  type: string
}

export interface CloudMachine {
  id: string
  host: string
  port: number | string
  username: string
}

export interface CloudProvider {
  id: string
  type?: string
  label: string
  type_label?: string
  homepage?: string
  configured: boolean
  last_error?: string | null
  model_dir?: string
  values?: Record<string, string>
  machines?: CloudMachine[]
}

export interface GpuConfiguration {
  gpu_count: number
  price_per_hour: number | null
  available?: boolean
  detail?: string
}
export interface GpuOffer {
  id: string
  label: string
  price_per_hour?: number | null
  available?: boolean
  configurations?: GpuConfiguration[]
}
export interface LaunchMethod {
  id: string
  label: string
  help?: string
  target_label: string
  searchable?: boolean
  configurable_gpu?: boolean
}
export interface LaunchOptions {
  methods: LaunchMethod[]
  gpus: GpuOffer[]
  gpu_counts: number[]
  capabilities?: string[]
}
export interface LaunchTarget {
  id: string
  label: string
  detail?: string
}
export interface LaunchPayload {
  provider: string
  method: string
  target_id: string
  target_label: string
  gpu_id: string
  gpu_label: string
  gpu_count: number
  interval_seconds: number
  services: string[]
  shutdown_mode: ShutdownMode
}

// ---- 影片修复 ----
export type RestoreState =
  | 'PREPARING' | 'WAITING' | 'DOWNLOADING' | 'PROCESSING' | 'UPLOADING' | 'FINALIZING'
  | 'COMPLETED' | 'FAILED' | 'CANCELLED'

export interface RestoreJob {
  id: string
  state: RestoreState
  restoration_model: string
  ltx_large_canvas?: boolean
  last_error?: string | null
  lease_owner?: string | null
  progress?: TransferProgress | null
  local_progress?: { stage?: string; detail?: string; total_bytes?: number; done_bytes?: number } | null
  output_path?: string | null
  backup_path?: string | null
  original_purged_at?: string | null
  reserved_machine?: string | null
  reserved_until?: string | null
  attempts?: number
}

export interface RestoreMedia {
  media_id: number
  path: string
  size: number
  library_name: string
  poster_url?: string | null
  updated_at: string
  status: ListStatus
  step?: string | null
  /** 说明，如“上次已取消，可重新提交。”“文件名里有修复标记”。 */
  status_reason?: string | null
  actions: RestoreMediaAction[]
  job?: RestoreJob | null
}

export interface RestoreMediaPage extends Page<RestoreMedia> {
  counts?: Partial<Record<ListView, number>>
}

export interface RestoreStatus {
  enabled: boolean
  heartbeat_timeout_seconds?: number
  models: Record<string, string>
  settings?: { restoration_model?: string; ltx_large_canvas?: boolean }
  workers: { name: string; last_seen_at: string; restore_models?: string[] }[]
  unavailable_workers?: { name: string; last_seen_at: string; reason: string }[]
}

export type RestoreAction = 'cancel' | 'retry' | 'purge_original'
export type RestoreMediaAction = 'restore' | RestoreAction

// ---- 总状态 ----
export interface ScanInfo {
  status?: string
  started_at?: string
  scanned?: number
  pending?: number
  completed?: number
  unusable?: number
  unsettled?: number
  relocated?: number
  errors?: unknown[]
  current_library?: string
  current_path?: string
}

export interface TranslationStatus {
  active_jobs: number
  job_concurrency: number
  inflight_requests: number
  request_concurrency: number
  request_count?: number
  fallback_count?: number
  retry_count?: number
  primary_available?: boolean
  upstreams?: Upstream[]
  fallback_upstreams?: Upstream[]
}

/** “添加显卡电脑”的候选接入地址（Docker 里不提供网卡地址）。 */
export interface WorkerConnectionOptions {
  worker_port: number
  addresses: string[]
  deployment: Deployment
}

/** 管理口令与当前会话。 */
export interface AuthStatus {
  configured: boolean
  authenticated: boolean
  min_password_length: number
  /** 这台设备能否设定口令：桌面版只能在运行 VideoTurbo 的电脑上设定。 */
  setup_allowed?: boolean
  /** 桌面版应用内窗口：不需要口令登录，也不显示“退出登录”。 */
  desktop_session?: boolean
}

/** 主控的部署方式：提示文案据此说明设置去哪里改。 */
export type Deployment = 'docker' | 'desktop' | 'source'

/** 桌面版外壳上报的本机识别状态。 */
export interface LocalWorker {
  phase: 'off' | 'pending' | 'waiting' | 'installing' | 'self_test' | 'running' | 'conflict' | 'need_tools' | 'failed' | string
  enabled: boolean
  ready: boolean
  /** 安装运行环境：第几步 / 共几步。 */
  index: number | null
  total: number | null
  zh: string
  en: string
  /** 安装或自检的最新一行输出。 */
  line: string
  message: string
  /** 控制台已请求开始下载，等外壳取走。 */
  download_requested: boolean
}

export interface Status {
  /** 主控的 VideoTurbo 版本。 */
  version: string
  deployment?: Deployment
  last_scan?: ScanInfo | null
  scan_status?: string
  manual_scan_enabled?: boolean
  pipeline_paused?: boolean
  warnings?: Warning[]
  media_libraries: { id: string; name: string; enabled: boolean }[]
  workers: Worker[]
  heartbeat_timeout_seconds?: number
  /** 桌面版：本机识别在识别程序连上之前的进度；没开启或已在运行时为 null。 */
  local_worker?: LocalWorker | null
  asr?: AsrSettings
  translation?: TranslationStatus
  ui?: UiSettings
  cloud?: CloudStatus
}

/** 插件仓库中的一个插件（来自仓库的 plugins.json，只用于展示；安装时以插件文件为准）。 */
export interface RepositoryPlugin {
  id: string
  label: string
  version: string
  description: string
  kinds: PluginKindInfo[]
  /** 插件文件地址。 */
  url: string
  /** 仓库公布的文件指纹，可为空。 */
  sha256: string
  icon: string
  homepage: string
}

/** 用户添加的插件仓库及其插件列表。 */
export interface PluginRepository {
  id: string
  /** 用户填写的仓库地址。 */
  url: string
  /** 实际读取的 plugins.json 地址。 */
  index_url: string
  name: string
  description?: string
  added_at: string
  /** 读取插件列表失败的原因。 */
  error: string | null
  plugins: RepositoryPlugin[]
  /** 格式不对、被跳过的条目数。 */
  skipped?: number
}

/** 顶部提醒；target 是点击后跳转的位置：页面名，或 settings:设置分类。 */
export interface Warning {
  message: string
  target?: string
  /** 红色（严重）样式。 */
  bad?: boolean
}
