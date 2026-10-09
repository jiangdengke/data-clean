import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { FormEvent, KeyboardEvent } from "react";
import "./styles.css";

type ConnectionProfile = { source_bucket: string; endpoint: string; credential_ref: string };
type ConnectionView = {
  configured: boolean;
  endpoint: string | null;
  source_buckets: string[];
  connections: ConnectionProfile[];
  runtime_ready?: boolean;
  readiness_message?: string;
};

type Progress = { total: number; processed: number; failed: number };

type JobStatus = {
  job_id: string;
  sync_job_id?: string;
  source_scan_job_id?: string;
  job_type?: string;
  status: string;
  source_buckets?: string[];
  current_source_bucket?: string | null;
  progress: Progress;
  report_available: boolean;
  error: string | null;
};

type ObjectReference = {
  key: string;
  size: number;
  etag?: string | null;
  last_modified?: string | null;
};
type ModelReport = { object_count: number; total_bytes: number; objects: ObjectReference[] };
type SourceBucketReport = {
  source_bucket: string;
  error: string | null;
  object_count: number;
  total_bytes: number;
  models: Record<string, ModelReport>;
  unmatched_objects: ObjectReference[];
  non_archive_objects: ObjectReference[];
  failed_objects: ObjectReference[];
  timed_out_objects: ObjectReference[];
};
type ScanReport = {
  job_id: string;
  generated_at: string;
  source_buckets: SourceBucketReport[];
  object_count: number;
  total_bytes: number;
};
type Mapping = { source_bucket: string; model_name: string; target_bucket: string };
type SyncPreflight = {
  success: boolean;
  reason: string;
  targets: { target_bucket: string; accessible: boolean; reason: string }[];
};
type SyncReport = {
  total: number;
  copied: number;
  skipped: number;
  failed: number;
};
type IncrementalStatus = {
  continuous_enabled: boolean;
  paused: boolean;
  runtime_ready: boolean;
  readiness_message: string;
  backfill: { status: string; total_seen?: number; total_classified?: number; source_bucket?: string | null; error_code?: string | null };
  counts: Record<string, number>;
  queue_last_pull_at: string | null;
  queue_last_ack_at: string | null;
  queue_backlog_count: number | null;
  reconcile_last_run_at: string | null;
  last_error: string | null;
};

type WorkflowStage = "connection" | "scan" | "mapping" | "operations";

const WORKFLOW_STAGES: { id: WorkflowStage; number: string; label: string }[] = [
  { id: "connection", number: "1", label: "源桶连接" },
  { id: "scan", number: "2", label: "扫描数据" },
  { id: "mapping", number: "3", label: "分流映射" },
  { id: "operations", number: "4", label: "同步与持续运行" },
];

function mergeReportMappings(report: ScanReport, savedMappings: Mapping[]): Mapping[] {
  const existing = new Map(savedMappings.map((mapping) => [`${mapping.source_bucket}\u0000${mapping.model_name}`, mapping.target_bucket]));
  return report.source_buckets.flatMap((bucket) => Object.keys(bucket.models).map((modelName) => ({
    source_bucket: bucket.source_bucket,
    model_name: modelName,
    target_bucket: existing.get(`${bucket.source_bucket}\u0000${modelName}`) ?? "",
  })));
}

function configuredSourceCount(connection: ConnectionView): number {
  if (!connection.configured) return 0;
  const buckets = connection.connections.length
    ? connection.connections.map((profile) => profile.source_bucket)
    : connection.source_buckets;
  return new Set(buckets.filter((bucket) => bucket.trim())).size;
}

function getInitialStage(connection: ConnectionView, report: ScanReport | null, mappings: Mapping[]): WorkflowStage {
  if (configuredSourceCount(connection) === 0) return "connection";
  if (!report) return "scan";
  return mappings.some((mapping) => !mapping.target_bucket.trim()) ? "mapping" : "operations";
}

async function callApi<ResponseBody>(path: string, init?: RequestInit): Promise<ResponseBody> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const errorBody = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof errorBody?.detail === "string" ? errorBody.detail : "Request failed";
    throw new Error(detail);
  }
  return (await response.json()) as ResponseBody;
}

function formatBytes(byteCount: number): string {
  if (byteCount < 1024) return `${byteCount} 字节`;
  if (byteCount < 1024 ** 2) return `${(byteCount / 1024).toFixed(1)} KB`;
  if (byteCount < 1024 ** 3) return `${(byteCount / 1024 ** 2).toFixed(1)} MB`;
  return `${(byteCount / 1024 ** 3).toFixed(2)} GB`;
}

function translateMessage(message: string, fallback: string): string {
  const translations: Record<string, string> = {
    "Invalid password": "密码不正确，请重试。",
    "Authentication required": "登录状态已失效，请重新登录。",
    "Cross-origin request rejected": "请求来源校验失败，请刷新页面后重试。",
    "Request failed": "请求失败，请稍后重试。",
    "At least one source bucket is required": "至少需要填写一个源存储桶。",
    "Source bucket, model name, and target bucket are required": "源存储桶、模型名称和目标存储桶都不能为空。",
    "Duplicate source bucket and model mapping": "源存储桶和模型的映射不能重复。",
    "Configure a source connection first": "请先填写并保存源存储桶连接信息。",
    "A scan is already running": "当前已有扫描任务正在运行。",
    "A sync is already running": "当前已有同步任务正在运行。",
    "Connection settings are not configured": "尚未配置连接信息。",
    "The scan report does not match the current source buckets": "扫描报告与当前源桶不一致，请重新扫描。",
    "The scan did not access every source bucket": "扫描未能访问全部源桶，请检查连接后重新扫描。",
    "Scan report not found": "找不到扫描报告，请重新扫描。",
    "Some discovered models do not have target buckets": "还有模型没有填写目标桶。",
    "Some target buckets are not accessible": "有目标桶无法访问，请检查桶名和权限。",
    "All target buckets are accessible": "目标桶检查通过，可以开始同步。",
    "Target bucket is accessible": "目标桶可访问。",
    "Target bucket is not accessible": "目标桶无法访问。",
    "Rclone transfer requires Cloudflare R2 HTTPS endpoints": "rclone 仅支持同一 Cloudflare R2 账户的 HTTPS endpoint。",
    "All source buckets are readable": "所有源桶连接成功，可以开始扫描。",
    "The scan failed before a report was generated": "扫描失败，未能生成报告。",
    "The sync failed before a report was generated": "同步失败，未能生成报告。",
    "The scan was interrupted by a service shutdown": "扫描因服务重启而中断，请重新启动。",
    "The sync was interrupted by a service shutdown": "同步因服务重启而中断，请重新启动。",
  };
  return translations[message] ?? fallback;
}

function getStatusLabel(status: string): string {
  return (
    {
      queued: "等待中",
      running: "运行中",
      completed: "已完成",
      interrupted: "已中断",
      failed: "失败",
    }[status] ?? "状态未知"
  );
}

function localizedError(error: unknown, fallback: string): string {
  return error instanceof Error ? translateMessage(error.message, fallback) : fallback;
}

function LoginScreen({ onLogin }: { onLogin: () => void }): React.JSX.Element {
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState("");

  async function submitLogin(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    try {
      await callApi("/api/login", { method: "POST", body: JSON.stringify({ password }) });
      onLogin();
    } catch (error) {
      setErrorMessage(localizedError(error, "登录失败，请稍后重试。"));
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card">
        <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">R2</span><span className="brand-name">模型同步</span></div>
        <div className="auth-copy"><p className="eyebrow">私有工作区</p><h1>让数据，一目了然。</h1><p>登录后配置源桶，识别模型并将对象同步到目标桶。</p></div>
        <form className="auth-form" onSubmit={submitLogin}>
          <label className="form-field" htmlFor="password"><span>管理员密码</span><input id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required placeholder="请输入管理员密码" /></label>
          {errorMessage && <p className="feedback feedback-error" role="alert">{errorMessage}</p>}
          <button className="primary-button" type="submit">登录</button>
        </form>
        <p className="auth-footnote"><span className="status-dot" aria-hidden="true" /> 凭据只保存在服务内存中</p>
      </section>
      <p className="auth-footer">R2 模型同步</p>
    </main>
  );
}

type ScanReportViewProps = { report: ScanReport };

function ScanReportView({ report }: ScanReportViewProps): React.JSX.Element {
  const modelCount = report.source_buckets.reduce((count, bucket) => count + Object.keys(bucket.models).length, 0);
  const failedCount = report.source_buckets.reduce((count, bucket) => count + bucket.failed_objects.length + bucket.timed_out_objects.length, 0);

  return (
    <section className="report-section" aria-labelledby="latest-report-title">
      <div className="report-heading">
        <div>
          <p className="section-kicker">最新报告</p>
          <h3 id="latest-report-title">扫描结果明细</h3>
        </div>
        <span className="report-complete">{modelCount} 个模型</span>
      </div>

      <div className="summary-grid" aria-label="扫描报告摘要">
        <div className="stat-item"><span className="stat-label">源对象</span><strong className="stat-value">{report.object_count}</strong></div>
        <div className="stat-item"><span className="stat-label">数据总量</span><strong className="stat-value">{formatBytes(report.total_bytes)}</strong></div>
        <div className="stat-item"><span className="stat-label">源桶</span><strong className="stat-value">{report.source_buckets.length}</strong></div>
        <div className="stat-item"><span className="stat-label">异常对象</span><strong className="stat-value">{failedCount}</strong></div>
      </div>

      <div className="report-bucket-list">
        {report.source_buckets.map((bucketReport) => (
          <details className="report-bucket" key={bucketReport.source_bucket}>
            <summary className="report-bucket-summary">
              <span className="report-bucket-title">
                <strong>{bucketReport.source_bucket}</strong>
                <small>{bucketReport.object_count} 个对象 · {Object.keys(bucketReport.models).length} 个模型 · {formatBytes(bucketReport.total_bytes)}</small>
              </span>
              <span className={`bucket-status ${bucketReport.error ? "is-error" : "is-ready"}`}>
                {bucketReport.error ? "读取失败" : "已识别"}
              </span>
            </summary>
            <div className="report-bucket-content">
              {bucketReport.error ? (
                <p className="feedback feedback-error">{translateMessage(bucketReport.error, "源桶访问失败，请检查连接后重试。")}</p>
              ) : Object.keys(bucketReport.models).length ? (
                <div className="report-model-list">
                  {Object.entries(bucketReport.models).map(([modelName, modelReport]) => (
                    <details className="report-model-row" key={modelName}>
                      <summary>
                        <span><strong>{modelName}</strong><small>{modelReport.object_count} 个对象 · {formatBytes(modelReport.total_bytes)}</small></span>
                        <span className="detail-action">查看对象</span>
                      </summary>
                      <ul className="object-list">
                        {modelReport.objects.map((sourceObject) => (
                          <li key={sourceObject.key}>
                            <code>{sourceObject.key}</code>
                            <span>{formatBytes(sourceObject.size)}</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ))}
                </div>
              ) : (
                <p className="empty-state">此源桶没有识别到可分流模型。</p>
              )}
              <p className="report-footnote">未匹配 {bucketReport.unmatched_objects.length} 项 · 非归档 {bucketReport.non_archive_objects.length} 项 · 失败 {bucketReport.failed_objects.length} 项 · 超时 {bucketReport.timed_out_objects.length} 项</p>
            </div>
          </details>
        ))}
      </div>
    </section>
  );
}

type MappingViewProps = {
  report: ScanReport;
  mappings: Mapping[];
  missingMappings: number;
  onMappingChange: (sourceBucket: string, modelName: string, targetBucket: string) => void;
  onSaveMappings: () => void;
};

function MappingView({ report, mappings, missingMappings, onMappingChange, onSaveMappings }: MappingViewProps): React.JSX.Element {
  const findMapping = (sourceBucket: string, modelName: string): Mapping | undefined => mappings.find((mapping) => mapping.source_bucket === sourceBucket && mapping.model_name === modelName);
  const mappedCount = mappings.length - missingMappings;

  return (
    <section className="mapping-workspace" aria-labelledby="mapping-workspace-title">
      <div className="mapping-heading">
        <div>
          <p className="section-kicker">映射状态</p>
          <h3 id="mapping-workspace-title">{mappedCount} / {mappings.length} 个模型已配置</h3>
        </div>
        <button className="primary-button" type="button" onClick={onSaveMappings} disabled={!mappings.length || missingMappings > 0}>保存映射</button>
      </div>

      {missingMappings > 0 && <p className="stage-notice is-warning">还有 {missingMappings} 个模型未填写目标桶。填写完成后保存，再到第 4 阶段检查目标桶。</p>}

      <div className="mapping-bucket-list">
        {report.source_buckets.map((bucketReport) => (
          <section className="mapping-bucket-group" key={bucketReport.source_bucket} aria-label={`${bucketReport.source_bucket} 模型映射`}>
            <div className="mapping-bucket-heading">
              <h4>{bucketReport.source_bucket}</h4>
              <span>{Object.keys(bucketReport.models).length} 个模型</span>
            </div>
            <div className="mapping-model-list">
              {Object.entries(bucketReport.models).map(([modelName, modelReport]) => {
                const mapping = findMapping(bucketReport.source_bucket, modelName);
                const isMapped = Boolean(mapping?.target_bucket.trim());
                return (
                  <label className="mapping-model-row" key={modelName}>
                    <span className="mapping-model-copy">
                      <span><strong>{modelName}</strong><span className={`bucket-status ${isMapped ? "is-ready" : "is-pending"}`}>{isMapped ? "已填写" : "未映射"}</span></span>
                      <small>{modelReport.object_count} 个对象 · {formatBytes(modelReport.total_bytes)}</small>
                    </span>
                    <span className="mapping-target-field">
                      <span>目标桶</span>
                      <input
                        value={mapping?.target_bucket ?? ""}
                        onChange={(event) => onMappingChange(bucketReport.source_bucket, modelName, event.target.value)}
                        placeholder={`例如：${bucketReport.source_bucket}-${modelName}`}
                      />
                    </span>
                  </label>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </section>
  );
}

type ConnectionProfilesProps = {
  connections: ConnectionProfile[];
  onApply: (index: number | null, profile: ConnectionProfile) => void;
  onRemove: (index: number) => void;
};

type ConnectionDraft = { index: number | null; profile: ConnectionProfile };

function ConnectionProfiles({ connections, onApply, onRemove }: ConnectionProfilesProps): React.JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const dialogId = useId();
  const bucketNameInputRef = useRef<HTMLInputElement>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLButtonElement | null>(null);
  const [draft, setDraft] = useState<ConnectionDraft | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const [formError, setFormError] = useState("");

  useEffect(() => {
    if (!draft || dialogRef.current?.open) return undefined;
    dialogRef.current?.showModal();
    const frame = window.requestAnimationFrame(() => bucketNameInputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [draft]);

  function openEditor(index: number | null, profile: ConnectionProfile, trigger: HTMLButtonElement): void {
    returnFocusRef.current = trigger;
    setRemovePending(false);
    setFormError("");
    setDraft({ index, profile: { ...profile } });
  }

  function closeEditor(): void {
    dialogRef.current?.close("cancel");
  }

  function handleDialogClose(): void {
    setDraft(null);
    setRemovePending(false);
    const returnTarget = returnFocusRef.current;
    window.requestAnimationFrame(() => {
      if (returnTarget?.isConnected) returnTarget.focus();
      else addButtonRef.current?.focus();
    });
  }

  function updateDraft(field: keyof ConnectionProfile, value: string): void {
    setFormError("");
    setDraft((current) => current ? { ...current, profile: { ...current.profile, [field]: value } } : current);
  }

  function applyDraft(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!draft) return;
    const sourceBucket = draft.profile.source_bucket.trim();
    const endpoint = draft.profile.endpoint.trim();
    const credentialRef = draft.profile.credential_ref.trim();
    if (!sourceBucket) {
      setFormError("源桶名称不能为空。");
      return;
    }
    if (!endpoint) {
      setFormError("R2 endpoint 不能为空。");
      return;
    }
    if (!credentialRef || !/^[A-Za-z0-9_-]+$/.test(credentialRef)) {
      setFormError("Secret 配置引用只能包含字母、数字、下划线或连字符。");
      return;
    }
    if (connections.some((profile, index) => index !== draft.index && profile.source_bucket.trim() === sourceBucket)) {
      setFormError("源桶名称不能重复。");
      return;
    }
    onApply(draft.index, {
      source_bucket: sourceBucket,
      endpoint,
      credential_ref: credentialRef,
    });
    dialogRef.current?.close("apply");
  }

  function confirmRemove(): void {
    if (draft?.index === null || draft?.index === undefined) return;
    onRemove(draft.index);
    dialogRef.current?.close("remove");
  }

  const draftName = draft?.profile.source_bucket.trim() || (draft?.index === null ? "新源桶" : "未命名源桶");
  const draftReady = Boolean(draft?.profile.source_bucket.trim() && draft.profile.endpoint.trim() && draft.profile.credential_ref.trim());

  return (
    <>
      <div className="connection-profiles" aria-label="源桶连接列表">
        {connections.map((profile, index) => {
          const bucketName = profile.source_bucket.trim() || `未命名源桶 ${index + 1}`;
          const endpointReady = Boolean(profile.endpoint.trim());
          const credentialReady = Boolean(profile.credential_ref.trim());
          const isConfigured = Boolean(profile.source_bucket.trim() && endpointReady && credentialReady);
          const endpointSummary = profile.endpoint.trim().replace(/^https?:\/\//, "") || "未设置";

          return (
            <button
              className="connection-profile-card"
              type="button"
              key={`connection-${index}`}
              aria-haspopup="dialog"
              aria-label={`编辑 ${bucketName} 源桶连接`}
              onClick={(event) => openEditor(index, profile, event.currentTarget)}
            >
              <span className="connection-profile-card-header">
                <span className="connection-profile-card-copy">
                  <span className="connection-profile-kicker">源桶 {index + 1}</span>
                  <span className="connection-profile-name">{bucketName}</span>
                </span>
                <span className={`bucket-status ${isConfigured ? "is-ready" : "is-pending"}`}>{isConfigured ? "已就绪" : "待完善"}</span>
              </span>
              <span className="connection-profile-meta" aria-label="连接摘要">
                <span><small>Endpoint</small><strong title={profile.endpoint}>{endpointSummary}</strong></span>
                <span><small>Secret 引用</small><strong>{credentialReady ? profile.credential_ref : "未设置"}</strong></span>
              </span>
              <span className="connection-profile-edit" aria-hidden="true">查看与编辑</span>
            </button>
          );
        })}
        <button
          ref={addButtonRef}
          className="connection-add-card"
          type="button"
          aria-haspopup="dialog"
          onClick={(event) => openEditor(null, { source_bucket: "", endpoint: "", credential_ref: "default" }, event.currentTarget)}
        >
          <span className="connection-add-mark" aria-hidden="true">+</span>
          <span>添加源桶</span>
          <small>创建新的连接配置</small>
        </button>
      </div>

      <dialog
        ref={dialogRef}
        className="connection-dialog"
        aria-labelledby={draft ? `${dialogId}-title` : undefined}
        onCancel={() => setRemovePending(false)}
        onClose={handleDialogClose}
        onClick={(event) => { if (event.target === event.currentTarget) closeEditor(); }}
      >
        {draft && (
          <div className="connection-dialog-surface">
            <form className="connection-dialog-form" onSubmit={applyDraft}>
              <header className="connection-dialog-header">
                <div>
                  <p className="section-kicker">源桶连接</p>
                  <h2 id={`${dialogId}-title`}>{draft.index === null ? "添加源桶" : `编辑 ${draftName}`}</h2>
                </div>
                <div className="connection-dialog-readiness" aria-live="polite">
                  <span className={`bucket-status ${draftReady ? "is-ready" : "is-pending"}`}>{draftReady ? "已就绪" : "待完善"}</span>
                  <span>{draftReady ? "连接字段已填写" : "请补全连接字段"}</span>
                </div>
              </header>

              {formError && <p className="feedback feedback-error connection-dialog-error" role="alert">{formError}</p>}

              <div className="connection-dialog-fields">
                <label className="form-field" htmlFor={`${dialogId}-source-bucket`}>
                  <span>源桶名称</span>
                  <input ref={bucketNameInputRef} id={`${dialogId}-source-bucket`} value={draft.profile.source_bucket} onChange={(event) => updateDraft("source_bucket", event.target.value)} placeholder="例如：source-a" autoComplete="off" required />
                </label>
                <label className="form-field" htmlFor={`${dialogId}-r2-endpoint`}>
                  <span>R2 endpoint</span>
                  <input id={`${dialogId}-r2-endpoint`} type="url" value={draft.profile.endpoint} onChange={(event) => updateDraft("endpoint", event.target.value)} placeholder="https://<account-id>.r2.cloudflarestorage.com" autoComplete="url" required />
                </label>
                <label className="form-field" htmlFor={`${dialogId}-credential-ref`}>
                  <span>Secret 配置引用</span>
                  <input id={`${dialogId}-credential-ref`} value={draft.profile.credential_ref} onChange={(event) => updateDraft("credential_ref", event.target.value)} placeholder="default" pattern="[A-Za-z0-9_-]+" autoComplete="off" required />
                  <small>仅填写部署 Secret 的引用名称；真实凭据不会在此显示。</small>
                </label>
              </div>

              {draft.index !== null && (
                <div className="connection-remove-zone">
                  {!removePending ? (
                    <button className="connection-profile-remove" type="button" onClick={() => setRemovePending(true)} disabled={connections.length <= 1}>
                      {connections.length <= 1 ? "至少保留一个源桶" : "移除源桶"}
                    </button>
                  ) : (
                    <div className="connection-remove-confirm" role="group" aria-label={`确认移除 ${draftName}`}>
                      <p><strong>确认移除此源桶？</strong><span>移除后仍需点击“保存连接”才会持久化。</span></p>
                      <div>
                        <button className="secondary-button" type="button" onClick={() => setRemovePending(false)}>保留</button>
                        <button className="danger-button" type="button" onClick={confirmRemove}>确认移除</button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              <footer className="connection-dialog-actions">
                <button className="secondary-button" type="button" onClick={closeEditor}>取消</button>
                <button className="primary-button" type="submit">应用</button>
              </footer>
            </form>
          </div>
        )}
      </dialog>
    </>
  );
}

function StageActions({ previous, next, onSelect }: { previous?: WorkflowStage; next?: WorkflowStage; onSelect: (stage: WorkflowStage) => void }): React.JSX.Element {
  const previousStage = previous ? WORKFLOW_STAGES.find((stage) => stage.id === previous) : undefined;
  const nextStage = next ? WORKFLOW_STAGES.find((stage) => stage.id === next) : undefined;
  return (
    <footer className="stage-actions">
      {previousStage ? <button className="secondary-button" type="button" onClick={() => onSelect(previousStage.id)}>上一步：{previousStage.label}</button> : <span />}
      {nextStage && <button className="primary-button" type="button" onClick={() => onSelect(nextStage.id)}>下一步：{nextStage.label}</button>}
    </footer>
  );
}

function Dashboard({ onLogout }: { onLogout: () => void }): React.JSX.Element {
  const [connection, setConnection] = useState<ConnectionView>({ configured: false, endpoint: null, source_buckets: [], connections: [] });
  const [connections, setConnections] = useState<ConnectionProfile[]>([{ source_bucket: "", endpoint: "", credential_ref: "default" }]);
  const [message, setMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [scanJob, setScanJob] = useState<JobStatus | null>(null);
  const [scanReport, setScanReport] = useState<ScanReport | null>(null);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [syncJob, setSyncJob] = useState<JobStatus | null>(null);
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);
  const [preflight, setPreflight] = useState<SyncPreflight | null>(null);
  const [incremental, setIncremental] = useState<IncrementalStatus | null>(null);
  const [activeStage, setActiveStage] = useState<WorkflowStage | null>(null);
  const stageWasManuallySelectedRef = useRef(false);
  const initialStageSelectedRef = useRef(false);

  function selectInitialStage(stage: WorkflowStage): void {
    if (initialStageSelectedRef.current || stageWasManuallySelectedRef.current) return;
    initialStageSelectedRef.current = true;
    setActiveStage(stage);
  }

  useEffect(() => {
    async function loadWorkspace(): Promise<void> {
      try {
        const [savedConnection, savedMappings, latestScan] = await Promise.all([
          callApi<ConnectionView>("/api/connection"),
          callApi<{ mappings: Mapping[] }>("/api/mappings"),
          callApi<JobStatus | null>("/api/scans/latest"),
        ]);
        let latestReport: ScanReport | null = null;
        if (latestScan?.report_available) {
          try {
            latestReport = await callApi<ScanReport>(`/api/scans/${latestScan.job_id}/report`);
          } catch (error) {
            setErrorMessage(localizedError(error, "无法读取扫描报告。"));
          }
        }
        const reportMappings = latestReport ? mergeReportMappings(latestReport, savedMappings.mappings) : savedMappings.mappings;
        setConnection(savedConnection);
        setConnections(savedConnection.connections.length ? savedConnection.connections : savedConnection.source_buckets.map((source_bucket) => ({ source_bucket, endpoint: savedConnection.endpoint ?? "", credential_ref: "default" })));
        setMappings(reportMappings);
        setScanJob(latestScan);
        setScanReport(latestReport);
        selectInitialStage(getInitialStage(savedConnection, latestReport, reportMappings));
      } catch (error) {
        setErrorMessage(localizedError(error, "无法读取工作区状态，请刷新页面重试。"));
        selectInitialStage("connection");
      }
    }
    void loadWorkspace();
    void callApi<JobStatus | null>("/api/sync/latest")
      .then(setSyncJob)
      .catch((error: unknown) => setErrorMessage((current) => current || localizedError(error, "无法读取最新同步状态。")));
    void callApi<IncrementalStatus>("/api/incremental/status")
      .then(setIncremental)
      .catch((error: unknown) => setErrorMessage((current) => current || localizedError(error, "无法读取持续分流状态。")));
  }, []);

  useEffect(() => {
    if (!scanJob || !["queued", "running"].includes(scanJob.status)) return undefined;
    const timer = window.setInterval(() => { void callApi<JobStatus>(`/api/scans/${scanJob.job_id}`).then(setScanJob).catch((error: unknown) => setErrorMessage(localizedError(error, "无法获取扫描进度。"))); }, 1500);
    return () => window.clearInterval(timer);
  }, [scanJob]);

  useEffect(() => {
    if (!scanJob?.report_available || scanReport?.job_id === scanJob.job_id) return;
    void callApi<ScanReport>(`/api/scans/${scanJob.job_id}/report`).then((report) => {
      setScanReport(report);
      setMappings((currentMappings) => mergeReportMappings(report, currentMappings));
    }).catch((error: unknown) => setErrorMessage(localizedError(error, "无法读取扫描报告。")));
  }, [scanJob?.job_id, scanJob?.report_available]);

  useEffect(() => {
    if (!syncJob || !["queued", "running"].includes(syncJob.status)) return undefined;
    const timer = window.setInterval(() => { void callApi<JobStatus>(`/api/sync/${syncJob.sync_job_id ?? syncJob.job_id}`).then(setSyncJob).catch((error: unknown) => setErrorMessage(localizedError(error, "无法获取同步进度。"))); }, 1500);
    return () => window.clearInterval(timer);
  }, [syncJob]);

  useEffect(() => {
    if (!syncJob?.report_available) return;
    void callApi<SyncReport>(`/api/sync/${syncJob.sync_job_id ?? syncJob.job_id}/report`).then(setSyncReport).catch((error: unknown) => setErrorMessage(localizedError(error, "无法读取同步报告。")));
  }, [syncJob?.job_id, syncJob?.report_available]);

  function applyConnection(index: number | null, profile: ConnectionProfile): void { setConnections((current) => index === null ? [...current, profile] : current.map((currentProfile, profileIndex) => profileIndex === index ? profile : currentProfile)); }
  function updateMapping(sourceBucket: string, modelName: string, targetBucket: string): void { setMappings((currentMappings) => currentMappings.map((mapping) => mapping.source_bucket === sourceBucket && mapping.model_name === modelName ? { ...mapping, target_bucket: targetBucket } : mapping)); setPreflight(null); }

  async function saveConnection(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    try { const saved = await callApi<ConnectionView>("/api/connection", { method: "POST", body: JSON.stringify({ connections }) }); setConnection(saved); setConnections(saved.connections); setScanReport(null); setPreflight(null); setSyncReport(null); setSyncJob(null); setErrorMessage(""); setMessage("源桶连接配置已保存。运行凭据仅由部署环境提供。"); } catch (error) { setErrorMessage(localizedError(error, "保存源桶配置失败，请检查填写内容。")); }
  }
  async function testConnection(): Promise<void> { try { const result = await callApi<{ success: boolean; reason: string }>("/api/connection/test", { method: "POST" }); if (result.success) { setErrorMessage(""); setMessage(translateMessage(result.reason, "连接测试已完成。")); } else { setMessage(""); setErrorMessage(translateMessage(result.reason, "连接测试失败，请检查连接信息。")); } } catch (error) { setErrorMessage(localizedError(error, "连接测试失败，请检查连接信息。")); } }
  async function startScan(): Promise<void> { try { const startedJob = await callApi<{ job_id: string; status: string }>("/api/scans", { method: "POST" }); setScanReport(null); setScanJob({ job_id: startedJob.job_id, status: startedJob.status, progress: { total: 0, processed: 0, failed: 0 }, report_available: false, error: null }); setMessage("扫描任务已启动。"); } catch (error) { setErrorMessage(localizedError(error, "无法启动扫描。")); } }
  async function saveMappings(): Promise<void> { try { const saved = await callApi<{ mappings: Mapping[] }>("/api/mappings", { method: "POST", body: JSON.stringify({ mappings }) }); setMappings(saved.mappings); setPreflight(null); setErrorMessage(""); setMessage("模型到目标桶的映射已保存。"); } catch (error) { setErrorMessage(localizedError(error, "保存映射失败。")); } }
  async function checkTargets(): Promise<void> { if (!scanJob) return; try { const result = await callApi<SyncPreflight>("/api/sync/preflight", { method: "POST", body: JSON.stringify({ scan_job_id: scanJob.job_id }) }); setPreflight(result); if (result.success) { setErrorMessage(""); setMessage(translateMessage(result.reason, result.reason)); } else { setMessage(""); setErrorMessage(translateMessage(result.reason, result.reason)); } } catch (error) { setMessage(""); setErrorMessage(localizedError(error, "目标桶检查失败。")); } }
  async function startSync(): Promise<void> { if (!scanJob) return; try { const startedJob = await callApi<{ sync_job_id: string; status: string }>("/api/sync", { method: "POST", body: JSON.stringify({ scan_job_id: scanJob.job_id }) }); setSyncReport(null); setSyncJob({ job_id: startedJob.sync_job_id, sync_job_id: startedJob.sync_job_id, status: startedJob.status, progress: { total: 0, processed: 0, failed: 0 }, report_available: false, error: null }); setMessage("同步任务已启动。已有对象会自动跳过，不会覆盖。"); } catch (error) { setErrorMessage(localizedError(error, "无法启动同步，请先通过目标桶检查。")); } }
  async function setContinuous(enabled: boolean): Promise<void> { try { const result = await callApi<IncrementalStatus>("/api/incremental/continuous", { method: "POST", body: JSON.stringify({ enabled }) }); setIncremental(result); setMessage(enabled ? "持续分流已开启。" : "持续分流已暂停。"); } catch (error) { setErrorMessage(localizedError(error, "无法更新持续分流状态。")); } }
  async function backfill(action: "start" | "pause" | "resume"): Promise<void> { try { const result = await callApi<IncrementalStatus>("/api/incremental/backfill", { method: "POST", body: JSON.stringify({ action }) }); setIncremental(result); setMessage("历史回填状态已更新。"); } catch (error) { setErrorMessage(localizedError(error, "无法更新历史回填。")); } }
  async function refreshIncremental(): Promise<void> { try { setIncremental(await callApi<IncrementalStatus>("/api/incremental/status")); } catch (error) { setErrorMessage(localizedError(error, "无法读取持续分流状态。")); } }
  async function logout(): Promise<void> { try { await callApi("/api/logout", { method: "POST" }); onLogout(); } catch (error) { setErrorMessage(localizedError(error, "退出登录失败。")); } }

  const scanActive = scanJob !== null && ["queued", "running"].includes(scanJob.status);
  const syncActive = syncJob !== null && ["queued", "running"].includes(syncJob.status);
  const scanProgress = scanJob && scanJob.progress.total > 0 ? Math.round((scanJob.progress.processed / scanJob.progress.total) * 100) : 0;
  const syncProgress = syncJob && syncJob.progress.total > 0 ? Math.round((syncJob.progress.processed / syncJob.progress.total) * 100) : 0;
  const missingMappings = useMemo(() => mappings.filter((mapping) => !mapping.target_bucket.trim()).length, [mappings]);
  const sourceCount = configuredSourceCount(connection);
  const mappedCount = mappings.length - missingMappings;

  function selectStage(stage: WorkflowStage): void {
    stageWasManuallySelectedRef.current = true;
    initialStageSelectedRef.current = true;
    setActiveStage(stage);
    window.requestAnimationFrame(() => document.getElementById(`stage-tab-${stage}`)?.focus());
  }

  function handleStageKeyDown(event: KeyboardEvent<HTMLButtonElement>, stage: WorkflowStage): void {
    const currentIndex = WORKFLOW_STAGES.findIndex((item) => item.id === stage);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % WORKFLOW_STAGES.length;
    else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + WORKFLOW_STAGES.length) % WORKFLOW_STAGES.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = WORKFLOW_STAGES.length - 1;
    else return;
    event.preventDefault();
    selectStage(WORKFLOW_STAGES[nextIndex].id);
  }

  const scanSummary = scanActive
    ? `${getStatusLabel(scanJob.status)} · ${scanProgress}%`
    : scanReport
      ? `${getStatusLabel(scanJob?.status ?? "completed")} · ${scanReport.object_count} 个对象`
      : scanJob
        ? getStatusLabel(scanJob.status)
        : "尚无报告";
  const mappingSummary = scanReport ? `${mappedCount} / ${mappings.length} 已配置` : "等待扫描报告";
  const operationSummary = syncActive
    ? `同步中 · ${syncProgress}%`
    : incremental === null
      ? "正在载入"
      : incremental.continuous_enabled
        ? "持续运行中"
        : "持续运行已暂停";

  return (
    <main className="page-shell">
      <header className="page-header">
        <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">R2</span><span className="brand-name">R2 模型同步</span></div>
        <div className="header-actions"><span className="session-indicator"><span className="status-dot" aria-hidden="true" /> 已登录</span><button className="secondary-button" type="button" onClick={() => void logout()}>退出</button></div>
      </header>

      <section className="workflow-summary" aria-label="工作流摘要">
        <div className="workflow-summary-item"><span>源桶</span><strong>{sourceCount ? `${sourceCount} 个已配置` : "尚未配置"}</strong></div>
        <div className="workflow-summary-item"><span>最新扫描</span><strong>{scanSummary}</strong></div>
        <div className="workflow-summary-item"><span>分流映射</span><strong>{mappingSummary}</strong></div>
        <div className="workflow-summary-item"><span>运行状态</span><strong>{operationSummary}</strong></div>
      </section>

      <nav className="workflow-tabs" role="tablist" aria-label="R2 数据分流工作流" aria-orientation="horizontal" aria-busy={activeStage === null}>
        {WORKFLOW_STAGES.map((stage) => {
          const isSelected = activeStage === stage.id;
          return (
            <button
              id={`stage-tab-${stage.id}`}
              className="workflow-tab"
              type="button"
              role="tab"
              aria-selected={isSelected}
              aria-controls={`stage-panel-${stage.id}`}
              tabIndex={isSelected ? 0 : -1}
              disabled={activeStage === null}
              key={stage.id}
              onClick={() => selectStage(stage.id)}
              onKeyDown={(event) => handleStageKeyDown(event, stage.id)}
            >
              <span className="workflow-tab-number" aria-hidden="true">{stage.number}</span>
              <span>{stage.label}</span>
            </button>
          );
        })}
      </nav>

      {errorMessage && <p className="feedback feedback-error page-feedback" role="alert">{errorMessage}</p>}
      {message && <p className="feedback feedback-success page-feedback" role="status">{message}</p>}

      {activeStage === null ? (
        <section className="workflow-workspace workflow-loading" aria-live="polite">正在载入工作流…</section>
      ) : (
        <>
          <section
            className="workflow-workspace"
            id="stage-panel-connection"
            role="tabpanel"
            aria-labelledby="stage-tab-connection"
            tabIndex={activeStage === "connection" ? 0 : -1}
            hidden={activeStage !== "connection"}
          >
              <header className="workspace-header">
                <div><p className="section-kicker">第 1 阶段</p><h1>源桶连接</h1><p>管理源桶、Endpoint 与部署 Secret 引用。真实凭据不会在页面中显示。</p></div>
                <span className={`connection-state ${connection.configured ? "is-configured" : ""}`}>{connection.configured ? "配置已保存" : "尚未保存"}</span>
              </header>
              {!connection.configured && <p className="stage-notice is-warning">先添加并保存至少一个完整连接；测试连接使用最近一次已保存的配置。</p>}
              <ConnectionProfiles connections={connections} onApply={applyConnection} onRemove={(index) => setConnections((current) => current.filter((_, profileIndex) => profileIndex !== index))} />
              <form className="workspace-toolbar" onSubmit={(event) => void saveConnection(event)}>
                <p>编辑器中的“应用”只更新当前页面，点击保存后才会持久化。</p>
                <div className="button-row">
                  <button className="primary-button" type="submit">保存连接</button>
                  <button className="secondary-button" type="button" onClick={() => void testConnection()} disabled={!connection.configured}>测试连接</button>
                </div>
              </form>
              <StageActions next="scan" onSelect={selectStage} />
          </section>

          <section
            className="workflow-workspace"
            id="stage-panel-scan"
            role="tabpanel"
            aria-labelledby="stage-tab-scan"
            tabIndex={activeStage === "scan" ? 0 : -1}
            hidden={activeStage !== "scan"}
          >
              <header className="workspace-header">
                <div><p className="section-kicker">第 2 阶段</p><h1>扫描数据</h1><p>手动读取 .tar.gz 归档并识别模型；扫描过程只读，不修改源对象。</p></div>
                {scanJob && <span className={`scan-state ${scanActive ? "is-running" : ""}`}>{getStatusLabel(scanJob.status)}</span>}
              </header>
              {!connection.configured && <p className="stage-notice is-warning">尚无已保存连接。仍可浏览此阶段，配置完成后才能启动扫描。</p>}
              <div className="scan-control-row">
                <div><strong>{scanReport ? `报告生成于 ${scanReport.generated_at}` : "尚无可用扫描报告"}</strong><span>{scanReport ? `${scanReport.source_buckets.length} 个源桶 · ${scanReport.object_count} 个对象` : "启动扫描后，进度与最新结果会显示在这里。"}</span></div>
                <button className="primary-button" type="button" onClick={() => void startScan()} disabled={!connection.configured || scanActive}>{scanActive ? "正在扫描…" : scanReport ? "重新扫描" : "开始扫描"}</button>
              </div>
              {scanJob && <div className="progress-panel" aria-live="polite"><div className="progress-heading"><span className="section-kicker">扫描进度</span><span className={`scan-state ${scanActive ? "is-running" : ""}`}>{getStatusLabel(scanJob.status)}</span></div>{scanJob.current_source_bucket && <p className="progress-caption">当前源桶：{scanJob.current_source_bucket}</p>}<div className="progress-meta"><strong>{scanProgress}%</strong><span>已处理 {scanJob.progress.processed} / {scanJob.progress.total || "等待统计"} 个对象</span></div><progress value={scanProgress} max="100">{scanProgress}%</progress><p className="progress-caption">已隔离 {scanJob.progress.failed} 个异常对象，其余对象会继续扫描。</p>{scanJob.error && <p className="feedback feedback-error">{translateMessage(scanJob.error, "扫描未能完成。")}</p>}</div>}
              {scanReport && <ScanReportView report={scanReport} />}
              <StageActions previous="connection" next="mapping" onSelect={selectStage} />
          </section>

          <section
            className="workflow-workspace"
            id="stage-panel-mapping"
            role="tabpanel"
            aria-labelledby="stage-tab-mapping"
            tabIndex={activeStage === "mapping" ? 0 : -1}
            hidden={activeStage !== "mapping"}
          >
              <header className="workspace-header">
                <div><p className="section-kicker">第 3 阶段</p><h1>分流映射</h1><p>为扫描识别出的每个模型指定目标桶；未映射模型会保留在待处理状态。</p></div>
                {scanReport && <span className={`connection-state ${missingMappings === 0 ? "is-configured" : ""}`}>{mappingSummary}</span>}
              </header>
              {scanReport ? (
                <MappingView report={scanReport} mappings={mappings} missingMappings={missingMappings} onMappingChange={updateMapping} onSaveMappings={() => void saveMappings()} />
              ) : (
                <div className="stage-empty"><strong>需要扫描报告</strong><p>先到“扫描数据”运行一次扫描，再返回配置模型到目标桶的映射。</p><button className="secondary-button" type="button" onClick={() => selectStage("scan")}>前往扫描数据</button></div>
              )}
              <StageActions previous="scan" next="operations" onSelect={selectStage} />
          </section>

          <section
            className="workflow-workspace"
            id="stage-panel-operations"
            role="tabpanel"
            aria-labelledby="stage-tab-operations"
            tabIndex={activeStage === "operations" ? 0 : -1}
            hidden={activeStage !== "operations"}
          >
              <header className="workspace-header">
                <div><p className="section-kicker">第 4 阶段</p><h1>同步与持续运行</h1><p>执行一次性同步，或管理新对象分流、历史回填和状态对账。</p></div>
                <span className={`connection-state ${incremental?.continuous_enabled ? "is-configured" : ""}`}>{operationSummary}</span>
              </header>

              <div className="operations-layout">
                <section className="operation-section" aria-labelledby="manual-sync-title">
                  <div className="operation-heading"><div><p className="section-kicker">手动同步</p><h2 id="manual-sync-title">预检与复制</h2></div><span>不覆盖已有对象</span></div>
                  <p className="operation-description">检查目标桶权限后，在 R2 内部复制完整对象并保留原 object key。</p>
                  {!scanReport && <p className="stage-notice is-warning">没有可用于同步的扫描报告，请先完成扫描。</p>}
                  {scanReport && missingMappings > 0 && <p className="stage-notice is-warning">还有 {missingMappings} 个模型未映射，请先在第 3 阶段补全并保存。</p>}
                  <div className="button-row">
                    <button className="secondary-button" type="button" onClick={() => void checkTargets()} disabled={!scanReport || !mappings.length || missingMappings > 0}>检查目标桶</button>
                    <button className="primary-button" type="button" onClick={() => void startSync()} disabled={!preflight?.success || syncActive}>{syncActive ? "正在同步…" : "开始同步"}</button>
                  </div>
                  {preflight && <div className="preflight-list">{preflight.targets.map((target) => <div className="preflight-row" key={target.target_bucket}><span className={`status-dot ${target.accessible ? "is-success" : "is-error"}`} aria-hidden="true" /><span>{target.target_bucket}</span><span>{target.accessible ? "可访问" : "无法访问"}</span></div>)}</div>}
                  {syncJob && <div className="progress-panel" aria-live="polite"><div className="progress-heading"><span className="section-kicker">同步进度</span><span className={`scan-state ${syncActive ? "is-running" : ""}`}>{getStatusLabel(syncJob.status)}</span></div><div className="progress-meta"><strong>{syncProgress}%</strong><span>已处理 {syncJob.progress.processed} / {syncJob.progress.total || "等待统计"} 个复制动作</span></div><progress value={syncProgress} max="100">{syncProgress}%</progress>{syncJob.error && <p className="feedback feedback-error">{translateMessage(syncJob.error, "同步未能完成。")}</p>}</div>}
                  {syncReport && <p className="report-footnote">共 {syncReport.total} 项 · 复制 {syncReport.copied} 项 · 跳过 {syncReport.skipped} 项 · 失败 {syncReport.failed} 项</p>}
                </section>

                <section className="operation-section" aria-labelledby="continuous-routing-title">
                  <div className="operation-heading"><div><p className="section-kicker">增量处理</p><h2 id="continuous-routing-title">持续分流</h2></div><label className="toggle-label"><input type="checkbox" checked={incremental?.continuous_enabled ?? false} onChange={(event) => void setContinuous(event.target.checked)} disabled={!incremental?.runtime_ready} /><span>自动分流</span></label></div>
                  <p className="operation-description">只读取新或变化的归档并按已保存映射复制；运行密钥始终来自部署环境。</p>
                  {incremental && !incremental.runtime_ready && <p className="stage-notice is-warning">{incremental.readiness_message}</p>}
                  <div className="incremental-summary"><span>待处理 <strong>{incremental?.counts.queued ?? 0}</strong></span><span>未映射 <strong>{incremental?.counts.unmapped ?? 0}</strong></span><span>失败 <strong>{incremental?.counts.failed ?? 0}</strong></span><span>冲突 <strong>{incremental?.counts.route_conflict ?? 0}</strong></span><span>队列积压 <strong>{incremental?.queue_backlog_count ?? "未知"}</strong></span></div>
                </section>

                <section className="operation-section operation-section-wide" aria-labelledby="backfill-title">
                  <div className="operation-heading"><div><p className="section-kicker">历史与对账</p><h2 id="backfill-title">回填、队列与对账</h2></div><button className="text-button" type="button" onClick={() => void refreshIncremental()}>刷新状态</button></div>
                  <p className="operation-description">历史回填需明确启动，可暂停和恢复；刷新会重新读取队列及最近对账状态。</p>
                  <div className="backfill-controls">
                    <div className="button-row"><button className="secondary-button" type="button" onClick={() => void backfill("start")}>开始回填</button><button className="secondary-button" type="button" onClick={() => void backfill(incremental?.backfill.status === "paused" ? "resume" : "pause")}>{incremental?.backfill.status === "paused" ? "恢复回填" : "暂停回填"}</button></div>
                    <div className="backfill-status"><span>回填状态 <strong>{incremental?.backfill.status ?? "idle"}</strong></span><span>已列举 <strong>{incremental?.backfill.total_seen ?? 0}</strong></span><span>已分类 <strong>{incremental?.backfill.total_classified ?? 0}</strong></span></div>
                  </div>
                  <dl className="runtime-timestamps"><div><dt>最近对账</dt><dd>{incremental?.reconcile_last_run_at ?? "尚未运行"}</dd></div><div><dt>队列拉取</dt><dd>{incremental?.queue_last_pull_at ?? "尚未运行"}</dd></div><div><dt>队列确认</dt><dd>{incremental?.queue_last_ack_at ?? "尚未运行"}</dd></div></dl>
                  {(incremental?.last_error || incremental?.backfill.error_code) && <p className="feedback feedback-error">最近错误：{incremental.last_error ?? incremental.backfill.error_code}</p>}
                </section>
              </div>
              <StageActions previous="mapping" onSelect={selectStage} />
          </section>
        </>
      )}
    </main>
  );
}

function App(): React.JSX.Element {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null);
  useEffect(() => { void callApi<{ authenticated: boolean }>("/api/session").then((session) => setIsAuthenticated(session.authenticated)).catch(() => setIsAuthenticated(false)); }, []);
  if (isAuthenticated === null) return <main className="auth-shell loading-state">正在载入…</main>;
  return isAuthenticated ? <Dashboard onLogout={() => setIsAuthenticated(false)} /> : <LoginScreen onLogin={() => setIsAuthenticated(true)} />;
}

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Application root element was not found");
createRoot(rootElement).render(<App />);
