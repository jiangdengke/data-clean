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
      <section className="card auth-card">
        <p className="eyebrow">Read-only R2 operations</p>
        <h1>R2 Model Scanner</h1>
        <p>Sign in with the configured administrator password.</p>
        <form onSubmit={submitLogin}>
          <label htmlFor="password">Administrator password</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
          {errorMessage && (
            <p className="error-message" role="alert">
              {errorMessage}
            </p>
          )}
          <button type="submit">Sign in</button>
        </form>
      </section>
    </main>
  );
}

function ReportView({ report }: { report: ScanReport }): React.JSX.Element {
  return (
    <section className="card report-card">
      <h2>Scan report</h2>
      <div className="summary-grid">
        <div>
          <span>Source objects</span>
          <strong>{report.object_count}</strong>
        </div>
        <div>
          <span>Total bytes</span>
          <strong>{formatBytes(report.total_bytes)}</strong>
        </div>
        <div>
          <span>Models found</span>
          <strong>{Object.keys(report.models).length}</strong>
        </div>
        <div>
          <span>Failures</span>
          <strong>
            {report.failed_objects.length + report.timed_out_objects.length}
          </strong>
        </div>
      </div>
      <h3>Models</h3>
      {Object.entries(report.models).map(([modelName, modelReport]) => (
        <details key={modelName}>
          <summary>
            {modelName}: {modelReport.object_count} objects,{" "}
            {formatBytes(modelReport.total_bytes)}
          </summary>
          <ul>
            {modelReport.objects.map((sourceObject) => (
              <li key={sourceObject.key}>
                <code>{sourceObject.key}</code> (
                {formatBytes(sourceObject.size)})
              </li>
            ))}
          </ul>
        </details>
      ))}
      <p>
        Unmatched archives: {report.unmatched_objects.length}; non-archives:{" "}
        {report.non_archive_objects.length}; failed:{" "}
        {report.failed_objects.length}; timed out:{" "}
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
    if (!jobStatus || !["queued", "running"].includes(jobStatus.status))
      return undefined;
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

  async function saveConnection(
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> {
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
        <div>
          <p className="eyebrow">Authenticated workspace</p>
          <h1>R2 Model Scanner</h1>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void logout()}
        >
          Sign out
        </button>
      </header>
      <section className="notice">
        <strong>Read-only slice:</strong> sync, upload, copy, overwrite, and
        delete operations are not enabled.
      </section>
      {errorMessage && (
        <p className="error-message" role="alert">
          {errorMessage}
        </p>
      )}
      <div className="dashboard-grid">
        <section className="card">
          <h2>Source connection</h2>
          <p className="muted">
            Credentials are never returned to the browser or persisted.
          </p>
          <form onSubmit={(event) => void saveConnection(event)}>
            <label htmlFor="endpoint">R2 endpoint</label>
            <input
              id="endpoint"
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
              required
            />
            <label htmlFor="access-key-id">Access key ID</label>
            <input
              id="access-key-id"
              value={accessKeyId}
              onChange={(event) => setAccessKeyId(event.target.value)}
              required
              autoComplete="off"
            />
            <label htmlFor="secret-access-key">Secret access key</label>
            <input
              id="secret-access-key"
              type="password"
              value={secretAccessKey}
              onChange={(event) => setSecretAccessKey(event.target.value)}
              required
              autoComplete="new-password"
            />
            <label htmlFor="source-bucket">Source bucket name</label>
            <input
              id="source-bucket"
              value={sourceBucket}
              onChange={(event) => setSourceBucket(event.target.value)}
              required
            />
            <div className="button-row">
              <button type="submit">Save in memory</button>
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
            <p className="success-message" role="status">
              {message}
            </p>
          )}
        </section>
        <section className="card">
          <h2>Read-only scan</h2>
          <p>
            Discover models from <code>roots/primary/&lt;model&gt;/</code>{" "}
            archive members.
          </p>
          <button
            type="button"
            onClick={() => void startScan()}
            disabled={!connection.configured || isActive}
          >
            Start scan
          </button>
          {jobStatus && (
            <div className="progress-panel" aria-live="polite">
              <p>
                <strong>Status:</strong> {jobStatus.status}
              </p>
              <progress value={progress} max="100">
                {progress}%
              </progress>
              <p>
                {jobStatus.progress.processed} of{" "}
                {jobStatus.progress.total || "unknown"} objects processed (
                {jobStatus.progress.failed} failures)
              </p>
              {jobStatus.error && (
                <p className="error-message">{jobStatus.error}</p>
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
