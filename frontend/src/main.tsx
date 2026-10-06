import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import "./styles.css";

type ConnectionView = {
  configured: boolean;
  endpoint: string | null;
  source_bucket: string | null;
};
type JobStatus = {
  job_id: string;
  status: string;
  source_bucket: string;
  progress: { total: number; processed: number; failed: number };
  report_available: boolean;
  error: string | null;
};
type ObjectReference = { key: string; size: number };
type ModelReport = {
  object_count: number;
  total_bytes: number;
  objects: ObjectReference[];
};
type ScanReport = {
  object_count: number;
  total_bytes: number;
  models: Record<string, ModelReport>;
  unmatched_objects: ObjectReference[];
  non_archive_objects: ObjectReference[];
  failed_objects: ObjectReference[];
  timed_out_objects: ObjectReference[];
};

async function callApi<ResponseBody>(
  path: string,
  init?: RequestInit,
): Promise<ResponseBody> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const errorBody = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(errorBody?.detail ?? "Request failed");
  }
  return (await response.json()) as ResponseBody;
}

function formatBytes(byteCount: number): string {
  if (byteCount < 1024) return `${byteCount} B`;
  if (byteCount < 1024 ** 2) return `${(byteCount / 1024).toFixed(1)} KiB`;
  if (byteCount < 1024 ** 3) return `${(byteCount / 1024 ** 2).toFixed(1)} MiB`;
  return `${(byteCount / 1024 ** 3).toFixed(2)} GiB`;
}

function LoginScreen({ onLogin }: { onLogin: () => void }): React.JSX.Element {
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState("");

  async function submitLogin(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    try {
      await callApi("/api/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      onLogin();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Login failed");
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            R2
          </span>
          <span className="brand-name">Model Scanner</span>
        </div>
        <div className="auth-copy">
          <p className="eyebrow">Private workspace</p>
          <h1>See your R2 data clearly.</h1>
          <p>Scan a source bucket, identify its models, and keep every result close at hand.</p>
        </div>
        <form className="auth-form" onSubmit={submitLogin}>
          <label className="form-field" htmlFor="password">
            <span>Administrator password</span>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          {errorMessage && (
            <p className="feedback feedback-error" role="alert">
              {errorMessage}
            </p>
          )}
          <button className="primary-button" type="submit">
            Continue
          </button>
        </form>
        <p className="auth-footnote">
          <span className="status-dot" aria-hidden="true" /> Read-only access. No R2 writes are performed.
        </p>
      </section>
      <p className="auth-footer">R2 Model Scanner</p>
    </main>
  );
}

function ReportView({ report }: { report: ScanReport }): React.JSX.Element {
  return (
    <section className="card report-card">
      <div className="card-header">
        <div>
          <p className="section-kicker">Latest result</p>
          <h2>Scan report</h2>
        </div>
        <span className="status-pill is-ready">Complete</span>
      </div>
      <div className="summary-grid">
        <div className="stat-card">
          <span className="stat-label">Source objects</span>
          <strong className="stat-value">{report.object_count}</strong>
        </div>
        <div className="stat-card">
          <span className="stat-label">Total bytes</span>
          <strong className="stat-value">{formatBytes(report.total_bytes)}</strong>
        </div>
        <div className="stat-card">
          <span className="stat-label">Models found</span>
          <strong className="stat-value">{Object.keys(report.models).length}</strong>
        </div>
        <div className="stat-card">
          <span className="stat-label">Failures</span>
          <strong className="stat-value">
            {report.failed_objects.length + report.timed_out_objects.length}
          </strong>
        </div>
      </div>
      <h3 className="section-title">Detected models</h3>
      <div className="model-list">
        {Object.entries(report.models).map(([modelName, modelReport]) => (
          <details className="model-detail" key={modelName}>
            <summary>
              <span className="model-name">{modelName}</span>
              <span className="model-meta">
                {modelReport.object_count} objects / {formatBytes(modelReport.total_bytes)}
              </span>
            </summary>
            <ul>
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
      <p className="report-footnote">
        Unmatched archives: {report.unmatched_objects.length}; non-archives:{" "}
        {report.non_archive_objects.length}; failed: {report.failed_objects.length}; timed out:{" "}
        {report.timed_out_objects.length}.
      </p>
    </section>
  );
}

function Dashboard({ onLogout }: { onLogout: () => void }): React.JSX.Element {
  const [connection, setConnection] = useState<ConnectionView>({
    configured: false,
    endpoint: null,
    source_bucket: null,
  });
  const [endpoint, setEndpoint] = useState("");
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [sourceBucket, setSourceBucket] = useState("");
  const [message, setMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [report, setReport] = useState<ScanReport | null>(null);

  useEffect(() => {
    void callApi<ConnectionView>("/api/connection")
      .then((savedConnection) => {
        setConnection(savedConnection);
        setEndpoint(savedConnection.endpoint ?? "");
        setSourceBucket(savedConnection.source_bucket ?? "");
      })
      .catch((error: unknown) =>
        setErrorMessage(
          error instanceof Error ? error.message : "Unable to load connection",
        ),
      );
  }, []);

  useEffect(() => {
    void callApi<JobStatus | null>("/api/scans/latest")
      .then((latestJob) => {
        if (latestJob) setJobStatus(latestJob);
      })
      .catch((error: unknown) =>
        setErrorMessage(
          error instanceof Error ? error.message : "Unable to load latest scan",
        ),
      );
  }, []);

  useEffect(() => {
    if (!jobStatus || !["queued", "running"].includes(jobStatus.status)) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      void callApi<JobStatus>(`/api/scans/${jobStatus.job_id}`)
        .then(setJobStatus)
        .catch((error: unknown) =>
          setErrorMessage(
            error instanceof Error ? error.message : "Unable to poll scan",
          ),
        );
    }, 1500);
    return () => window.clearInterval(timer);
  }, [jobStatus]);

  useEffect(() => {
    if (!jobStatus?.report_available) return;
    void callApi<ScanReport>(`/api/scans/${jobStatus.job_id}/report`)
      .then(setReport)
      .catch((error: unknown) =>
        setErrorMessage(
          error instanceof Error ? error.message : "Unable to load report",
        ),
      );
  }, [jobStatus?.job_id, jobStatus?.report_available]);

  async function saveConnection(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    try {
      const savedConnection = await callApi<ConnectionView>("/api/connection", {
        method: "POST",
        body: JSON.stringify({
          endpoint,
          access_key_id: accessKeyId,
          secret_access_key: secretAccessKey,
          source_bucket: sourceBucket,
        }),
      });
      setConnection(savedConnection);
      setAccessKeyId("");
      setSecretAccessKey("");
      setMessage("Connection settings stored in server memory only.");
    } catch (error) {
      setErrorMessage(
        error instanceof Error ? error.message : "Unable to save connection",
      );
    }
  }

  async function testConnection(): Promise<void> {
    try {
      const result = await callApi<{ reason: string }>("/api/connection/test", {
        method: "POST",
      });
      setMessage(result.reason);
    } catch (error) {
      setErrorMessage(
        error instanceof Error ? error.message : "Connection test failed",
      );
    }
  }

  async function startScan(): Promise<void> {
    try {
      const startedJob = await callApi<{ job_id: string; status: string }>(
        "/api/scans",
        { method: "POST" },
      );
      setReport(null);
      setJobStatus({
        job_id: startedJob.job_id,
        status: startedJob.status,
        source_bucket: sourceBucket,
        progress: { total: 0, processed: 0, failed: 0 },
        report_available: false,
        error: null,
      });
    } catch (error) {
      setErrorMessage(
        error instanceof Error ? error.message : "Unable to start scan",
      );
    }
  }

  async function logout(): Promise<void> {
    await callApi("/api/logout", { method: "POST" });
    onLogout();
  }

  const isActive =
    jobStatus !== null && ["queued", "running"].includes(jobStatus.status);
  const progress =
    jobStatus && jobStatus.progress.total > 0
      ? Math.round(
          (jobStatus.progress.processed / jobStatus.progress.total) * 100,
        )
      : 0;

  return (
    <main className="page-shell">
      <header className="page-header">
        <div className="brand-and-title">
          <div className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              R2
            </span>
            <span className="brand-name">Model Scanner</span>
          </div>
          <div className="title-divider" aria-hidden="true" />
          <div>
            <p className="eyebrow">Authenticated workspace</p>
            <h1>Model Scanner</h1>
          </div>
        </div>
        <div className="header-actions">
          <span className="status-pill is-active">
            <span className="status-dot" aria-hidden="true" /> Session active
          </span>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void logout()}
          >
            Sign out
          </button>
        </div>
      </header>

      <section className="page-intro">
        <p className="section-kicker">Source intelligence</p>
        <h2>A clear view of your source bucket.</h2>
        <p>Connect once, scan deterministically, and review every model path without moving any data.</p>
      </section>

      <section className="notice" role="status">
        <span className="notice-mark" aria-hidden="true">
          i
        </span>
        <div>
          <strong>Read-only mode</strong>
          <p>Sync, upload, copy, overwrite, and delete operations are not enabled.</p>
        </div>
      </section>

      {errorMessage && (
        <p className="feedback feedback-error page-feedback" role="alert">
          {errorMessage}
        </p>
      )}

      <div className="dashboard-grid">
        <section className="card workflow-card">
          <div className="card-header">
            <div>
              <p className="section-kicker">Step 01</p>
              <h2>Source connection</h2>
            </div>
            <span className={`status-pill ${connection.configured ? "is-ready" : "is-muted"}`}>
              {connection.configured ? "Ready" : "Not set"}
            </span>
          </div>
          <p className="card-description">
            Credentials are never returned to the browser or persisted.
          </p>
          <form className="connection-form" onSubmit={(event) => void saveConnection(event)}>
            <div className="field-grid">
              <label className="form-field" htmlFor="endpoint">
                <span>R2 endpoint</span>
                <input
                  id="endpoint"
                  value={endpoint}
                  onChange={(event) => setEndpoint(event.target.value)}
                  required
                />
              </label>
              <label className="form-field" htmlFor="access-key-id">
                <span>Access key ID</span>
                <input
                  id="access-key-id"
                  value={accessKeyId}
                  onChange={(event) => setAccessKeyId(event.target.value)}
                  required
                  autoComplete="off"
                />
              </label>
              <label className="form-field" htmlFor="secret-access-key">
                <span>Secret access key</span>
                <input
                  id="secret-access-key"
                  type="password"
                  value={secretAccessKey}
                  onChange={(event) => setSecretAccessKey(event.target.value)}
                  required
                  autoComplete="new-password"
                />
              </label>
              <label className="form-field" htmlFor="source-bucket">
                <span>Source bucket name</span>
                <input
                  id="source-bucket"
                  value={sourceBucket}
                  onChange={(event) => setSourceBucket(event.target.value)}
                  required
                />
              </label>
            </div>
            <div className="button-row">
              <button className="primary-button" type="submit">
                Save in memory
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => void testConnection()}
                disabled={!connection.configured}
              >
                Test read access
              </button>
            </div>
          </form>
          {message && (
            <p className="feedback feedback-success" role="status">
              {message}
            </p>
          )}
        </section>

        <section className="card workflow-card">
          <div className="card-header">
            <div>
              <p className="section-kicker">Step 02</p>
              <h2>Read-only scan</h2>
            </div>
            <span className="status-pill is-muted">Deterministic</span>
          </div>
          <p className="card-description">
            Discover models from archive members under the configured source bucket.
          </p>
          <div className="scan-rule">
            <span className="scan-rule-label">Matching path</span>
            <code>roots/primary/&lt;model&gt;/</code>
          </div>
          <button
            className="primary-button wide-button"
            type="button"
            onClick={() => void startScan()}
            disabled={!connection.configured || isActive}
          >
            {isActive ? "Scan in progress" : "Start scan"}
          </button>
          {!connection.configured && (
            <p className="action-note">Save a source connection before starting a scan.</p>
          )}
          {jobStatus && (
            <div className="progress-panel" aria-live="polite">
              <div className="progress-heading">
                <span className="section-kicker">Current scan</span>
                <span className={`status-pill ${isActive ? "is-active" : "is-ready"}`}>
                  {jobStatus.status}
                </span>
              </div>
              <div className="progress-meta">
                <strong>{progress}%</strong>
                <span>
                  {jobStatus.progress.processed} of {jobStatus.progress.total || "unknown"} objects
                </span>
              </div>
              <progress value={progress} max="100">
                {progress}%
              </progress>
              <p className="progress-caption">
                {jobStatus.progress.failed} failures isolated from the scan.
              </p>
              {jobStatus.error && (
                <p className="feedback feedback-error">{jobStatus.error}</p>
              )}
            </div>
          )}
        </section>
      </div>
      {report && <ReportView report={report} />}
    </main>
  );
}

function App(): React.JSX.Element {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null);
  useEffect(() => {
    void callApi<{ authenticated: boolean }>("/api/session")
      .then((session) => setIsAuthenticated(session.authenticated))
      .catch(() => setIsAuthenticated(false));
  }, []);
  if (isAuthenticated === null)
    return <main className="auth-shell">Loading...</main>;
  return isAuthenticated ? (
    <Dashboard onLogout={() => setIsAuthenticated(false)} />
  ) : (
    <LoginScreen onLogin={() => setIsAuthenticated(true)} />
  );
}

export default App;
