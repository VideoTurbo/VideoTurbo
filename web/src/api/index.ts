// 主控接口。每个函数对应一个后端路由，界面代码不直接拼 URL。
import { del, get, post, put, seg } from './http'
import type {
  AppConfig, AsrSettings, AuthStatus, BatchResult, CloudInstanceGroup, CloudOrphanProfile, CloudProvider, CloudProviderType,
  CloudSettings, ConfigValues, ConnectionKind, ConnectionPayload, JobDetail, JobPage, Launch, LaunchOptions, LaunchPayload, LaunchTarget,
  FieldValue, FolderPage, LibraryPayload, MediaLibrary, MediaSourceOption, NodePayload, Notice, NoticeSummary, PluginConnection, PluginList, PluginPreview, PluginRepository, PluginResult, PluginSettingsInfo, PluginType, PluginUpdateCheck, RestoreAction, RestoreMediaPage,
  RestoreStatus, ShutdownMode, Status, UiSettings,
} from './types'

export * from './types'
export { ApiError, errorMessage } from './http'

type Query = Record<string, string | number | undefined>
function query(params: Query): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  return search.toString()
}

export const api = {
  status: () => get<Status>('/api/status'),

  // 字幕任务
  jobs: (params: Query) => get<JobPage>(`/api/jobs?${query(params)}`),
  job: (id: string) => get<JobDetail>(`/api/jobs/${seg(id)}`),
  jobFileUrl: (id: string, kind: string) => `/api/jobs/${seg(id)}/files/${seg(kind)}`,
  jobAction: (id: string, action: string, body?: unknown) => post<unknown>(`/api/jobs/${seg(id)}/${action}`, body),
  jobBatch: (jobIds: string[], action: string) =>
    post<BatchResult>('/api/jobs/batch/action', { job_ids: jobIds, action }),

  // 扫描与流水线
  startScan: (libraryIds: string[]) => post<unknown>('/api/scan', { library_ids: libraryIds }),
  cancelScan: () => post<unknown>('/api/scan/cancel'),
  resumePipeline: () => post<unknown>('/api/pipeline/resume'),

  // Worker
  deleteWorker: (name: string) => del<unknown>(`/api/workers/${seg(name)}`),

  // 影片修复
  restoreStatus: () => get<RestoreStatus>('/api/restore/status'),
  restoreMedia: (params: Query) => get<RestoreMediaPage>(`/api/restore/media?${query(params)}`),
  createRestoreJobs: (body: { media_ids: number[]; restoration_model: string; ltx_large_canvas: boolean }) =>
    post<{ created: unknown[]; skipped?: { reason: string }[] }>('/api/restore/jobs', body),
  restoreJobAction: (id: string, action: RestoreAction) =>
    post<{ message?: string }>(`/api/restore/jobs/${seg(id)}/actions/${action}`),
  restoreBatch: (jobIds: string[], action: RestoreAction) =>
    post<BatchResult>('/api/restore/batch', { job_ids: jobIds, action }),

  // 翻译节点
  config: () => get<AppConfig>('/api/config'),
  saveConfigSection: (section: string, body: ConfigValues) => put<unknown>(`/api/config/${section}`, body),

  // 媒体库：逐个立即保存；来源和文件夹只在添加时选择。
  libraries: () => get<MediaLibrary[]>('/api/libraries'),
  /** 添加媒体库时可选的网盘插件。 */
  librarySources: () => get<MediaSourceOption[]>('/api/libraries/sources'),
  createLibrary: (body: LibraryPayload & { source: string; path: string }) => post<{ id: string }>('/api/libraries', body),
  updateLibrary: (id: string, body: LibraryPayload) => put<unknown>(`/api/libraries/${seg(id)}`, body),
  deleteLibrary: (id: string) => del<{ media_removed: number }>(`/api/libraries/${seg(id)}`),
  browseFolders: (source: string, path?: string | null) =>
    get<FolderPage>(`/api/libraries/browse?${query({ source, path: path ?? undefined })}`),
  createNode: (body: NodePayload) => post<unknown>('/api/llm/nodes', body),
  updateNode: (id: string, body: NodePayload) => put<unknown>(`/api/llm/nodes/${seg(id)}`, body),
  deleteNode: (id: string) => del<unknown>(`/api/llm/nodes/${seg(id)}`),
  listModels: (body: unknown) => post<{ models: string[] }>('/api/llm/models', body),
  enableUpstream: (name: string) => post<unknown>(`/api/translation/upstreams/${seg(name)}/enable`),
  probeUpstream: (name: string) =>
    post<{ compatible: boolean; error?: string }>(`/api/translation/upstreams/${seg(name)}/probe`),

  // 运行设置
  asrSettings: () => get<AsrSettings>('/api/settings/asr'),
  saveAsrSettings: (body: AsrSettings) => put<unknown>('/api/settings/asr', body),
  transferSettings: () => get<{ upload_limit_mb: number | null }>('/api/settings/transfer'),
  saveTransferSettings: (limit: number | null) => put<unknown>('/api/settings/transfer', { upload_limit_mb: limit }),
  uiSettings: () => get<UiSettings>('/api/settings/ui'),
  saveUiSettings: (body: UiSettings) => put<unknown>('/api/settings/ui', body),
  cloudSettings: () => get<CloudSettings>('/api/settings/cloud'),
  saveCloudSettings: (body: Omit<CloudSettings, 'jasna_license_set'> & { jasna_license: string | null }) =>
    put<CloudSettings>('/api/settings/cloud', body),
  clearJasnaLicense: () => del<CloudSettings>('/api/settings/cloud/jasna-license'),
  workerToken: () => get<{ token: string }>('/api/settings/worker-token'),
  resetWorkerToken: () => post<{ token: string }>('/api/settings/worker-token/reset'),

  // 账户
  authStatus: () => get<AuthStatus>('/api/auth/status'),
  setupPassword: (password: string) => post<unknown>('/api/auth/setup', { password }),
  changePassword: (current: string, next: string) =>
    post<unknown>('/api/auth/password', { current_password: current, new_password: next }),
  logout: () => post<unknown>('/api/auth/logout'),

  // 云平台
  cloudInstances: () => get<CloudInstanceGroup[]>('/api/cloud/instances'),
  cloudInstanceAction: (provider: string, id: string, action: 'shutdown' | 'destroy' | 'forget') =>
    post<unknown>(`/api/cloud/instances/${seg(provider)}/${seg(id)}/${action}`),
  bindCloud: (machine: string, body: { provider: string; instance_id: string; shutdown_mode: ShutdownMode }) =>
    put<unknown>(`/api/cloud/bindings/${seg(machine)}`, body),
  unbindCloud: (machine: string) => del<unknown>(`/api/cloud/bindings/${seg(machine)}`),
  setShutdownMode: (machine: string, mode: ShutdownMode) =>
    put<unknown>(`/api/cloud/bindings/${seg(machine)}/shutdown-mode`, { shutdown_mode: mode }),
  cloudProviders: () => get<CloudProvider[]>('/api/cloud/providers'),
  cloudProviderTypes: () => get<CloudProviderType[]>('/api/cloud/provider-types'),
  testCloudProvider: (id: string, values: Record<string, FieldValue>) =>
    post<{ message: string }>(`/api/cloud/providers/${seg(id)}/test`, { values }),
  testCloudProviderType: (type: string, values: Record<string, FieldValue>) =>
    post<{ message: string }>(`/api/cloud/provider-types/${seg(type)}/test`, { values }),
  createCloudProvider: (body: unknown) => post<unknown>('/api/cloud/providers', body),
  updateCloudProvider: (id: string, body: unknown) => put<unknown>(`/api/cloud/providers/${seg(id)}`, body),
  deleteCloudProvider: (id: string) => del<unknown>(`/api/cloud/providers/${seg(id)}`),
  cloudOrphanProviders: () => get<CloudOrphanProfile[]>('/api/cloud/orphan-providers'),

  // 插件：安装和更新都分两步，先取 SHA-256 给用户核对（不执行），确认后带着它提交。
  plugins: () => get<PluginList>('/api/plugins'),
  previewPlugin: (url: string) => post<PluginPreview>('/api/plugins/preview', { url }),
  installPlugin: (url: string, sha256: string, iconSha256 = '') => post<PluginResult>('/api/plugins', { url, sha256, icon_sha256: iconSha256 }),
  previewUploadedPlugin: (filename: string, source: string) => post<PluginPreview>('/api/plugins/upload/preview', { filename, source }),
  installUploadedPlugin: (filename: string, source: string, sha256: string, iconSha256 = '') =>
    post<PluginResult>('/api/plugins/upload', { filename, source, sha256, icon_sha256: iconSha256 }),
  checkPluginUpdate: (id: string) => post<PluginUpdateCheck>(`/api/plugins/${seg(id)}/update/check`),
  updatePlugin: (id: string, sha256: string, iconSha256 = '') => post<PluginResult>(`/api/plugins/${seg(id)}/update`, { sha256, icon_sha256: iconSha256 }),
  uninstallPlugin: (id: string) => del<unknown>(`/api/plugins/${seg(id)}`),
  pluginRepositories: () => get<{ repositories: PluginRepository[] }>('/api/plugin-repositories'),
  addPluginRepository: (url: string) => post<PluginRepository>('/api/plugin-repositories', { url }),
  removePluginRepository: (id: string) => del<unknown>(`/api/plugin-repositories/${seg(id)}`),
  // 插件设置：所有配置共用；网盘插件的账号等也在这里填写。
  pluginSettings: (id: string) => get<PluginSettingsInfo>(`/api/plugins/${seg(id)}/settings`),
  savePluginSettings: (id: string, values: Record<string, FieldValue>) =>
    put<{ saved: boolean; changed: boolean }>(`/api/plugins/${seg(id)}/settings`, { values }),
  testPluginSettings: (id: string, values: Record<string, FieldValue>) =>
    post<{ message: string }>(`/api/plugins/${seg(id)}/settings/test`, { values }),

  // 媒体服务器与通知的配置
  connectionTypes: (kind: ConnectionKind) => get<PluginType[]>(`/api/${kind}/types`),
  testConnectionType: (kind: ConnectionKind, type: string, values: Record<string, FieldValue>) =>
    post<{ message: string }>(`/api/${kind}/types/${seg(type)}/test`, { values }),
  connections: (kind: ConnectionKind) => get<PluginConnection[]>(`/api/${kind}`),
  createConnection: (kind: ConnectionKind, body: ConnectionPayload) => post<{ id: string }>(`/api/${kind}`, body),
  updateConnection: (kind: ConnectionKind, id: string, body: ConnectionPayload) => put<unknown>(`/api/${kind}/${seg(id)}`, body),
  deleteConnection: (kind: ConnectionKind, id: string) => del<unknown>(`/api/${kind}/${seg(id)}`),
  testConnection: (kind: ConnectionKind, id: string, values: Record<string, FieldValue>) =>
    post<{ message: string }>(`/api/${kind}/${seg(id)}/test`, { values }),

  // 创建实例
  launchOptions: (provider: string) => get<LaunchOptions>(`/api/cloud/providers/${seg(provider)}/launch-options`),
  launchTargets: (provider: string, method: string, q: string) =>
    get<LaunchTarget[]>(`/api/cloud/providers/${seg(provider)}/launch-targets?${new URLSearchParams({ method, q })}`),
  createLaunch: (body: LaunchPayload) => post<unknown>('/api/cloud/launches', body),
  launch: (id: string) => get<Launch>(`/api/cloud/launches/${seg(id)}`),
  launchLogUrl: (id: string) => `/api/cloud/launches/${seg(id)}/log`,

  // 诊断包：版本、去掉密钥的设置、GPU 机器、插件与最近日志，反馈问题时附在 Issue 里。
  diagnosticsUrl: '/api/diagnostics',
  cancelLaunch: (id: string) => post<unknown>(`/api/cloud/launches/${seg(id)}/cancel`),
  redeployLaunch: (id: string, forgetHostKey: boolean) =>
    post<unknown>(`/api/cloud/launches/${seg(id)}/redeploy${forgetHostKey ? '?forget_host_key=true' : ''}`),
  continueLaunchAsr: (id: string) => post<unknown>(`/api/cloud/launches/${seg(id)}/continue-asr`),
  deleteLaunch: (id: string) => del<unknown>(`/api/cloud/launches/${seg(id)}`),

  // 提醒
  notices: (limit = 100) => get<NoticeSummary & { items: Notice[] }>(`/api/notices?limit=${limit}`),
  noticesAfter: (afterId: number) => get<{ items: Notice[] }>(`/api/notices?after_id=${afterId}`),
  markNoticesRead: () => post<NoticeSummary>('/api/notices/read'),
}
