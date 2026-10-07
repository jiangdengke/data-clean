import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import type { FormEvent } from "react";
import "./styles.css";

type ConnectionView = {
  configured: boolean;
  endpoint: string | null;
  source_buckets: string[];
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

type ObjectReference = { key: string; size: number };
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

type ReportViewProps = {
  report: ScanReport;
  mappings: Mapping[];
  missingMappings: number;
  preflight: SyncPreflight | null;
  onMappingChange: (sourceBucket: string, modelName: string, targetBucket: string) => void;
  onSaveMappings: () => void;
  onCheckTargets: () => void;
};

function ReportView({
  report,
  mappings,
  missingMappings,
  preflight,
  onMappingChange,
  onSaveMappings,
  onCheckTargets,
}: ReportViewProps): React.JSX.Element {
  const modelCount = report.source_buckets.reduce((count, bucket) => count + Object.keys(bucket.models).length, 0);
  const failedCount = report.source_buckets.reduce((count, bucket) => count + bucket.failed_objects.length + bucket.timed_out_objects.length, 0);
  const findMapping = (sourceBucket: string, modelName: string): Mapping | undefined => mappings.find((mapping) => mapping.source_bucket === sourceBucket && mapping.model_name === modelName);

  return (
    <section className="source-report-section">
      <div className="report-overview">
        <div className="report-overview-heading">
          <div>
            <p className="section-kicker">扫描结果</p>
            <h2>源桶与模型</h2>
          </div>
          <span className="report-complete">{modelCount} 个模型待分流</span>
        </div>
        <div className="summary-grid">
          <div className="stat-card"><span className="stat-label">源对象</span><strong className="stat-value">{report.object_count}</strong></div>
          <div className="stat-card"><span className="stat-label">数据总量</span><strong className="stat-value">{formatBytes(report.total_bytes)}</strong></div>
          <div className="stat-card"><span className="stat-label">源桶</span><strong className="stat-value">{report.source_buckets.length}</strong></div>
          <div className="stat-card"><span className="stat-label">异常对象</span><strong className="stat-value">{failedCount}</strong></div>
        </div>
      </div>

      <div className="source-bucket-grid">
        {report.source_buckets.map((bucketReport) => (
          <article className="source-bucket-card" key={bucketReport.source_bucket}>
            <div className="source-bucket-header">
              <div>
                <p className="section-kicker">源桶</p>
                <h3>{bucketReport.source_bucket}</h3>
              </div>
              <span className={`bucket-status ${bucketReport.error ? "is-error" : "is-ready"}`}>
                {bucketReport.error ? "读取失败" : "已识别"}
              </span>
            </div>
            <div className="source-bucket-stats">
              <span>{bucketReport.object_count} 个对象</span>
              <span>{Object.keys(bucketReport.models).length} 个模型</span>
              <span>{formatBytes(bucketReport.total_bytes)}</span>
            </div>
            {bucketReport.error ? (
              <p className="feedback feedback-error">{translateMessage(bucketReport.error, "源桶访问失败，请检查连接后重试。")}</p>
            ) : (
              <div className="model-card-grid">
                {Object.entries(bucketReport.models).map(([modelName, modelReport]) => {
                  const mapping = findMapping(bucketReport.source_bucket, modelName);
                  return (
                    <article className="model-card" key={modelName}>
                      <div className="model-card-header">
                        <div>
                          <p className="model-card-label">模型</p>
                          <h4>{modelName}</h4>
                        </div>
                        <span className="model-meta">{modelReport.object_count} 个对象</span>
                      </div>
                      <label className="model-target-field" htmlFor={`target-${bucketReport.source_bucket}-${modelName}`}>
                        <span>目标桶</span>
                        <input
                          id={`target-${bucketReport.source_bucket}-${modelName}`}
                          value={mapping?.target_bucket ?? ""}
                          onChange={(event) => onMappingChange(bucketReport.source_bucket, modelName, event.target.value)}
                          placeholder={`例如：${bucketReport.source_bucket} ${modelName}`}
                        />
                      </label>
                      <details className="object-list">
                        <summary>查看对象 · {formatBytes(modelReport.total_bytes)}</summary>
                        <ul>
                          {modelReport.objects.map((sourceObject) => (
                            <li key={sourceObject.key}>
                              <code>{sourceObject.key}</code>
                              <span>{formatBytes(sourceObject.size)}</span>
                            </li>
                          ))}
                        </ul>
                      </details>
                    </article>
                  );
                })}
              </div>
            )}
            <p className="report-footnote">未匹配 {bucketReport.unmatched_objects.length} 项 · 非归档 {bucketReport.non_archive_objects.length} 项 · 失败 {bucketReport.failed_objects.length} 项 · 超时 {bucketReport.timed_out_objects.length} 项</p>
          </article>
        ))}
      </div>

      <div className="mapping-toolbar">
        <div>
          <p className="section-kicker">目标桶映射</p>
          <p>{missingMappings ? `还有 ${missingMappings} 项模型未填写目标桶` : "所有模型都已填写目标桶"}</p>
        </div>
        <div className="button-row">
          <button className="primary-button" type="button" onClick={onSaveMappings} disabled={!mappings.length || missingMappings > 0}>保存映射</button>
          <button className="secondary-button" type="button" onClick={onCheckTargets} disabled={!mappings.length || missingMappings > 0}>检查目标桶</button>
        </div>
        {preflight && <div className="preflight-list">{preflight.targets.map((target) => <div className="preflight-row" key={target.target_bucket}><span className={`status-dot ${target.accessible ? "is-success" : "is-error"}`} aria-hidden="true" /><span>{target.target_bucket}</span><span>{target.accessible ? "可访问" : "无法访问"}</span></div>)}</div>}
      </div>
    </section>
  );
}

function Dashboard({ onLogout }: { onLogout: () => void }): React.JSX.Element {
  const [connection, setConnection] = useState<ConnectionView>({ configured: false, endpoint: null, source_buckets: [] });
  const [endpoint, setEndpoint] = useState("");
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [sourceBuckets, setSourceBuckets] = useState<string[]>([""]);
  const [message, setMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [scanJob, setScanJob] = useState<JobStatus | null>(null);
  const [scanReport, setScanReport] = useState<ScanReport | null>(null);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [syncJob, setSyncJob] = useState<JobStatus | null>(null);
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);
  const [preflight, setPreflight] = useState<SyncPreflight | null>(null);

  useEffect(() => {
    void Promise.all([callApi<ConnectionView>("/api/connection"), callApi<{ mappings: Mapping[] }>("/api/mappings"), callApi<JobStatus | null>("/api/scans/latest"), callApi<JobStatus | null>("/api/sync/latest")]).then(([savedConnection, savedMappings, latestScan, latestSync]) => {
      setConnection(savedConnection); setEndpoint(savedConnection.endpoint ?? ""); setSourceBuckets(savedConnection.source_buckets.length ? savedConnection.source_buckets : [""]); setMappings(savedMappings.mappings); if (latestScan) setScanJob(latestScan); if (latestSync) setSyncJob(latestSync);
    }).catch((error: unknown) => setErrorMessage(localizedError(error, "无法读取工作区状态，请刷新页面重试。")));
  }, []);

  useEffect(() => {
    if (!scanJob || !["queued", "running"].includes(scanJob.status)) return undefined;
    const timer = window.setInterval(() => { void callApi<JobStatus>(`/api/scans/${scanJob.job_id}`).then(setScanJob).catch((error: unknown) => setErrorMessage(localizedError(error, "无法获取扫描进度。"))); }, 1500);
    return () => window.clearInterval(timer);
  }, [scanJob]);

  useEffect(() => {
    if (!scanJob?.report_available) return;
    void callApi<ScanReport>(`/api/scans/${scanJob.job_id}/report`).then((report) => { setScanReport(report); setMappings((currentMappings) => { const existing = new Map(currentMappings.map((mapping) => [`${mapping.source_bucket}\u0000${mapping.model_name}`, mapping.target_bucket])); return report.source_buckets.flatMap((bucket) => Object.keys(bucket.models).map((modelName) => ({ source_bucket: bucket.source_bucket, model_name: modelName, target_bucket: existing.get(`${bucket.source_bucket}\u0000${modelName}`) ?? "" }))); }); }).catch((error: unknown) => setErrorMessage(localizedError(error, "无法读取扫描报告。")));
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

  function updateSourceBucket(index: number, value: string): void { setSourceBuckets((currentBuckets) => currentBuckets.map((bucket, bucketIndex) => bucketIndex === index ? value : bucket)); }
  function updateMapping(sourceBucket: string, modelName: string, targetBucket: string): void { setMappings((currentMappings) => currentMappings.map((mapping) => mapping.source_bucket === sourceBucket && mapping.model_name === modelName ? { ...mapping, target_bucket: targetBucket } : mapping)); setPreflight(null); }

  async function saveConnection(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    try { const saved = await callApi<ConnectionView>("/api/connection", { method: "POST", body: JSON.stringify({ endpoint, access_key_id: accessKeyId, secret_access_key: secretAccessKey, source_buckets: sourceBuckets }) }); setConnection(saved); setSourceBuckets(saved.source_buckets); setAccessKeyId(""); setSecretAccessKey(""); setScanReport(null); setPreflight(null); setSyncReport(null); setSyncJob(null); setErrorMessage(""); setMessage("连接信息已保存。密钥只在服务运行期间保留。"); } catch (error) { setErrorMessage(localizedError(error, "保存连接信息失败，请检查填写内容。")); }
  }
  async function testConnection(): Promise<void> { try { const result = await callApi<{ success: boolean; reason: string }>("/api/connection/test", { method: "POST" }); if (result.success) { setErrorMessage(""); setMessage(translateMessage(result.reason, "连接测试已完成。")); } else { setMessage(""); setErrorMessage(translateMessage(result.reason, "连接测试失败，请检查连接信息。")); } } catch (error) { setErrorMessage(localizedError(error, "连接测试失败，请检查连接信息。")); } }
  async function startScan(): Promise<void> { try { const startedJob = await callApi<{ job_id: string; status: string }>("/api/scans", { method: "POST" }); setScanReport(null); setScanJob({ job_id: startedJob.job_id, status: startedJob.status, progress: { total: 0, processed: 0, failed: 0 }, report_available: false, error: null }); setMessage("扫描任务已启动。"); } catch (error) { setErrorMessage(localizedError(error, "无法启动扫描。")); } }
  async function saveMappings(): Promise<void> { try { const saved = await callApi<{ mappings: Mapping[] }>("/api/mappings", { method: "POST", body: JSON.stringify({ mappings }) }); setMappings(saved.mappings); setPreflight(null); setErrorMessage(""); setMessage("模型到目标桶的映射已保存。"); } catch (error) { setErrorMessage(localizedError(error, "保存映射失败。")); } }
  async function checkTargets(): Promise<void> { if (!scanJob) return; try { const result = await callApi<SyncPreflight>("/api/sync/preflight", { method: "POST", body: JSON.stringify({ scan_job_id: scanJob.job_id }) }); setPreflight(result); setMessage(translateMessage(result.reason, result.reason)); } catch (error) { setErrorMessage(localizedError(error, "目标桶检查失败。")); } }
  async function startSync(): Promise<void> { if (!scanJob) return; try { const startedJob = await callApi<{ sync_job_id: string; status: string }>("/api/sync", { method: "POST", body: JSON.stringify({ scan_job_id: scanJob.job_id }) }); setSyncReport(null); setSyncJob({ job_id: startedJob.sync_job_id, sync_job_id: startedJob.sync_job_id, status: startedJob.status, progress: { total: 0, processed: 0, failed: 0 }, report_available: false, error: null }); setMessage("同步任务已启动。已有对象会自动跳过，不会覆盖。"); } catch (error) { setErrorMessage(localizedError(error, "无法启动同步，请先通过目标桶检查。")); } }
  async function logout(): Promise<void> { try { await callApi("/api/logout", { method: "POST" }); onLogout(); } catch (error) { setErrorMessage(localizedError(error, "退出登录失败。")); } }

  const scanActive = scanJob !== null && ["queued", "running"].includes(scanJob.status);
  const syncActive = syncJob !== null && ["queued", "running"].includes(syncJob.status);
  const scanProgress = scanJob && scanJob.progress.total > 0 ? Math.round((scanJob.progress.processed / scanJob.progress.total) * 100) : 0;
  const syncProgress = syncJob && syncJob.progress.total > 0 ? Math.round((syncJob.progress.processed / syncJob.progress.total) * 100) : 0;
  const missingMappings = useMemo(() => mappings.filter((mapping) => !mapping.target_bucket.trim()).length, [mappings]);

  return (
    <main className="page-shell">
      <header className="page-header"><div className="brand-lockup"><span className="brand-mark" aria-hidden="true">R2</span><span className="brand-name">R2 模型同步</span></div><div className="header-actions"><span className="session-indicator"><span className="status-dot" aria-hidden="true" /> 已登录</span><button className="secondary-button" type="button" onClick={() => void logout()}>退出</button></div></header>
      <section className="page-intro"><p className="section-kicker">数据工作区</p><h1>源桶工作区</h1><p>按源桶查看模型，为每个模型设置目标桶，然后执行同步。</p></section>
      <p className="read-only-note"><span className="status-dot" aria-hidden="true" /> 扫描阶段只读；只有点击“开始同步”后才会执行复制。</p>
      {errorMessage && <p className="feedback feedback-error page-feedback" role="alert">{errorMessage}</p>}
      {message && <p className="feedback feedback-success page-feedback" role="status">{message}</p>}
      <div className="dashboard-grid">
        <section className="card workflow-card"><div className="card-header"><div><p className="section-kicker">01 / 连接</p><h2>配置源存储桶</h2></div><span className={`connection-state ${connection.configured ? "is-configured" : ""}`}>{connection.configured ? "已配置" : "未配置"}</span></div><p className="card-description">多个源桶共用同一组 R2 连接信息。请先手动创建目标桶。</p><form className="connection-form" onSubmit={(event) => void saveConnection(event)}><div className="field-grid"><label className="form-field" htmlFor="endpoint"><span>R2 服务地址</span><input id="endpoint" value={endpoint} onChange={(event) => setEndpoint(event.target.value)} required placeholder="https://<account-id>.r2.cloudflarestorage.com" /></label><label className="form-field" htmlFor="access-key-id"><span>访问密钥 ID</span><input id="access-key-id" value={accessKeyId} onChange={(event) => setAccessKeyId(event.target.value)} required autoComplete="off" /></label><label className="form-field" htmlFor="secret-access-key"><span>私密访问密钥</span><input id="secret-access-key" type="password" value={secretAccessKey} onChange={(event) => setSecretAccessKey(event.target.value)} required autoComplete="new-password" /></label></div><div className="bucket-inputs"><div className="form-field"><span>源存储桶名称</span>{sourceBuckets.map((bucket, index) => <div className="bucket-input-row" key={`source-${index}`}><input aria-label={`源存储桶 ${index + 1}`} value={bucket} onChange={(event) => updateSourceBucket(index, event.target.value)} placeholder={index === 0 ? "例如：招 2" : "例如：招 3"} />{sourceBuckets.length > 1 && <button className="icon-button" type="button" aria-label={`删除源桶 ${index + 1}`} onClick={() => setSourceBuckets((currentBuckets) => currentBuckets.filter((_, bucketIndex) => bucketIndex !== index))}>−</button>}</div>)}<button className="text-button" type="button" onClick={() => setSourceBuckets((currentBuckets) => [...currentBuckets, ""])}>+ 添加源桶</button></div></div><div className="button-row"><button className="primary-button" type="submit">保存连接</button><button className="secondary-button" type="button" onClick={() => void testConnection()} disabled={!connection.configured}>测试连接</button></div></form></section>
        <section className="card workflow-card"><div className="card-header"><div><p className="section-kicker">02 / 扫描</p><h2>识别每个源桶里的模型</h2></div></div><p className="card-description">按对象顺序读取 .tar.gz 归档，只读取成员路径，不解压、不修改源对象。</p><div className="scan-rule"><span className="scan-rule-label">识别路径</span><code>roots/primary/&lt;model&gt;/</code></div><button className="primary-button wide-button" type="button" onClick={() => void startScan()} disabled={!connection.configured || scanActive}>{scanActive ? "正在扫描…" : "开始扫描"}</button>{scanJob && <div className="progress-panel" aria-live="polite"><div className="progress-heading"><span className="section-kicker">扫描进度</span><span className={`scan-state ${scanActive ? "is-running" : ""}`}>{getStatusLabel(scanJob.status)}</span></div>{scanJob.current_source_bucket && <p className="progress-caption">当前源桶：{scanJob.current_source_bucket}</p>}<div className="progress-meta"><strong>{scanProgress}%</strong><span>已处理 {scanJob.progress.processed} / {scanJob.progress.total || "等待统计"} 个对象</span></div><progress value={scanProgress} max="100">{scanProgress}%</progress><p className="progress-caption">已隔离 {scanJob.progress.failed} 个异常对象，其余对象会继续扫描。</p></div>}</section>
      </div>
      {scanReport && <ReportView report={scanReport} mappings={mappings} missingMappings={missingMappings} preflight={preflight} onMappingChange={updateMapping} onSaveMappings={() => void saveMappings()} onCheckTargets={() => void checkTargets()} />}
      {scanReport && <section className="card sync-card"><div className="card-header"><div><p className="section-kicker">04 / 运行</p><h2>开始同步</h2></div><span className="connection-state">不会覆盖已有对象</span></div><p className="card-description">程序会在 R2 内部复制完整对象，保留原 object key。目标桶已有同名对象时自动跳过。</p><button className="primary-button wide-button" type="button" onClick={() => void startSync()} disabled={!preflight?.success || syncActive}>{syncActive ? "正在同步…" : "开始同步"}</button>{!preflight?.success && <p className="action-note">请先保存完整映射，并点击“检查目标桶”。</p>}{syncJob && <div className="progress-panel" aria-live="polite"><div className="progress-heading"><span className="section-kicker">同步进度</span><span className={`scan-state ${syncActive ? "is-running" : ""}`}>{getStatusLabel(syncJob.status)}</span></div><div className="progress-meta"><strong>{syncProgress}%</strong><span>已处理 {syncJob.progress.processed} / {syncJob.progress.total || "等待统计"} 个复制动作</span></div><progress value={syncProgress} max="100">{syncProgress}%</progress>{syncJob.error && <p className="feedback feedback-error">{translateMessage(syncJob.error, "同步未能完成。")}</p>}</div>}{syncReport && <p className="report-footnote">复制 {syncReport.copied} 项 · 跳过 {syncReport.skipped} 项 · 失败 {syncReport.failed} 项</p>}</section>}
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
