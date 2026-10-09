# 设计大规模 R2 增量分流方案

## Goal

为 1–2 TB 且持续有新对象写入的 R2 源桶实现可靠的增量模型分流：只做一次可控的历史回填，之后通过 R2 事件发现新对象、读取新归档一次、按已保存映射复制完整归档，并对账、重试和监控。

## What I already know

- 当前扫描先列举每个源桶的全部对象，再对每个 `.tar.gz` 对象执行读取，通过归档成员路径识别模型。
- 当前同步只复制扫描报告中的对象；它不会自动发现扫描完成后新入桶的对象。
- 当前目标行为是：识别模型后复制完整归档到模型对应的目标桶，不拆分归档内部数据。
- 源桶可能达到 1–2 TB，重复读取所有归档会产生明显时间、请求和数据传输成本。
- 已完成事件驱动 + 持久索引 + 低频对账的架构研究；用户确认按该方案继续。
- 用户选择部署注入 R2/Queue 连续运行凭据、映射后自动复制、历史回填由管理员显式启动、目标已存在时跳过并告警；持续处理启用状态在重启后自动恢复。

## Research References

- [`research/r2-object-events.md`](research/r2-object-events.md) — R2 `object-create` → Queue → HTTP pull，以及 DLQ 和定期对账建议。
- [`research/incremental-indexing.md`](research/incremental-indexing.md) — 大桶增量索引、对象指纹、历史回填和幂等处理建议。

## Decision (ADR-lite)

**Context**：源桶可能有 1–2 TB，模型名只存在 `.tar.gz` 内部；重复全量读取会很慢，但新对象必须及时发现。

**Decision**：采用“事件发现为主、元数据对账为辅、对象版本索引持久化”的方案。R2 `object-create` 事件进入 Cloudflare Queue，由应用通过 HTTP pull 接收；应用先把事件写入 SQLite，再处理单个新归档。定时 `ListObjectsV2` 只做元数据对账，发现漏事件或覆盖版本。第一次仍需对历史数据做一次可断点全量回填。

**Consequences**：日常不再重复读取历史归档；每个新或变化的归档仍需读取一次以识别模型。需要 Cloudflare Queue/DLQ、连续运行凭据和 SQLite 状态库。保留当前整包 CopyObject、不覆盖目标对象和手动映射边界。

## Questions to resolve

- 连续运行凭据：部署通过环境/Secret 注入，绝不写入数据卷。
- 自动分流：持续模式开启后，已保存映射的对象自动复制；未映射模型进入待配置状态，不猜目标桶。
- 首次历史回填：由管理员显式启动；使用有界并发、进度、checkpoint 和暂停/恢复。
- 新版本覆盖同名源 key 时，跳过已有目标对象并告警，不覆盖；保留冲突供管理员处理。

## Requirements (confirmed)

- 使用 R2 `object-create` -> Cloudflare Queue -> HTTP pull 作为主要新对象发现路径；使用低频 `ListObjectsV2` 元数据对账弥补漏事件。
- SQLite（WAL）保存对象版本索引、模型识别结果、处理状态、重试时间和路由任务；不要把高频工作队列写成大型 JSON 文件。
- 对象身份按 `(source_bucket, object_key, fingerprint)` 去重；ETag + size 用作版本线索，不把 ETag 当作通用 MD5。
- 事件先持久化到 SQLite，再确认 Queue；归档读取和复制在 Queue acknowledgment 之后独立处理。
- 每个新/变化 `.tar.gz` 读取归档一次以识别内部模型；保持 `roots/primary/<model>/` 的现有识别逻辑，不解压落盘。
- 识别到模型后按已保存 `(source bucket, model)` 映射自动复制完整归档；未配置映射时保留待处理状态，不猜测目标桶。
- 保留源对象，不创建桶，不覆盖已有目标对象；同 key 的新源版本遇到已存在目标时记录冲突并等待管理员处理。
- 网络/超时错误使用有界重试；损坏归档作为可见终态；重复事件和重启恢复都必须幂等。
- 首次历史回填由管理员明确启动，提供有界并发、进度、checkpoint、暂停/恢复；事件发现必须在回填之前启用，回填与新事件合并并在回填后对账。
- 持续处理由管理员明确启用；该开关持久化，服务重启后自动恢复此前开启的状态；管理员可随时暂停，暂停不丢失已接收任务。
- R2 和 Queue 凭据从部署环境或 Secret Manager 注入，永不写入数据卷、报告或日志；管理界面不能回显它们。
- UI 展示运行/暂停状态、回填进度、待处理/失败计数、最近对账时间及未映射模型；保留手动全量扫描作为恢复工具。

## Acceptance Criteria

- [x] SQLite schema 与事务测试覆盖对象版本去重、事件重复、状态转换、重试和重启恢复。
- [x] 历史回填可分页、有限并发、可 checkpoint、暂停/恢复，且重复运行不重复读取已分类版本。
- [x] 新事件写入索引后才 ack；重复和乱序事件不会错误覆盖新版本状态。
- [x] 元数据对账能够发现索引中没有的对象和变化 fingerprint，且不读取已分类归档。
- [x] 已识别且有映射的新对象自动整包复制；无映射对象等待配置。
- [x] 目标已有对象保持不覆盖并显示版本冲突；不创建目标桶、不删除源数据。
- [x] 退避重试、有界本地失败、队列积压、队列时间戳、失败和对账时间均可观测；DLQ 配置与外部队列重试监控需在 Cloudflare 部署阶段完成。
- [x] UI 可启动/暂停持续分流、启动/暂停/恢复历史回填并查看工作状态。
- [x] 凭据只从部署 Secret 注入，不出现在 SQLite 业务数据、报告和日志中。
- [x] 单元/集成测试可用 mock 验证完整链路，不触达真实 R2 或 Cloudflare Queue。
- [x] 提供 Cloudflare Queue、DLQ、R2 event rule 和安全部署 Secret 的配置说明。
- [x] The optional rclone transfer adapter is packaged as pinned, checksum-verified `v1.74.3` for Linux `amd64`/`arm64`, remains opt-in behind the `boto3` default, and has deployment, readiness, limitation, and rollback documentation.
- [x] 不自动对真实 1–2 TB 桶启动回填，不直接修改 Cloudflare 资源，不部署线上或执行真实复制。

## Remaining Limitations

- Queue pull/ack, payload decoding, metadata listing, backfill and copy decision paths have local mock coverage; this implementation has not performed live provider integration validation.
- DLQ provisioning/consumption, queue retry/dead-letter metrics, R2 event notification rules, and production Secret Manager wiring remain deployment work. The application does not create or modify those Cloudflare resources.
- Target existence uses a HEAD-then-transfer sequence. Both adapters skip keys observed as existing, but the provider API sequence is not an atomic conditional create; concurrent external writes can race that check. Rclone also cannot bind its copy to the source fingerprint checked by the preceding HEAD, and post-copy size verification cannot establish which writer won.
- Rclone is limited to configless, same-account `copyto` using one source profile. It fails closed for unsupported endpoints/topologies and objects over Cloudflare R2's 5 GiB `CopyObject` limit; there is no streamed or multipart-copy fallback in the current mode.
- A successful rclone exit can represent a skip. The adapter uses bounded structured output for classification and verifies target size, but no positive copied record is conservatively reported as skipped.
- Historical backfill is paginated and checkpointed but currently uses a single durable classifier worker; continuous worker concurrency does not fan out backfill classification.
- Restart recovery applies to the SQLite incremental queues and persisted continuous-mode toggle. Legacy queued/running scan and manual sync jobs are still marked interrupted on service restart.
- No real 1–2 TB backfill, provider deployment, or source-to-target copy was run during this implementation; the verification suite uses mocks and dummy Compose environment values only.

## Definition of Done

- 增量扫描/分流应用代码、迁移/持久化、UI、测试与部署说明完成。
- 后端测试、前端 lint/build、静态检查通过。
- Optional rclone transfer deployment is complete: the production image, non-secret sample settings, opt-in/rollback procedure, and operational limitations are documented. `boto3` remains the production-compatible default; live provider validation remains outstanding.
- Final trellis-check passed all 65 backend tests, Python compilation, frontend lint/build, Compose interpolation with dummy values, published rclone checksum verification, and `git diff --check`. Docker image build validation remains unavailable because the local Docker daemon is stopped.
- 用户能在本地/测试环境配置凭据并明确控制持续运行和历史回填。
- Cloudflare 资源配置与线上启用步骤有文档，但需独立操作，不由本实现自动创建资源、部署或触发真实回填/复制。

## Out of Scope

- 不修改归档格式，不拆分 `.tar.gz`，不清洗归档内部内容。
- 不创建或改动用户 Cloudflare Queue、DLQ、R2 event notification 资源。
- 不配置/写入线上 Secret，不部署线上服务。
- 不对真实源桶执行历史回填，不对真实目标桶执行复制。

## Technical Notes

- 当前发现逻辑：`backend/scanner.py`。
- 当前对象列举和 CopyObject：`backend/r2_client.py`、`backend/sync.py`。
- 当前持久化：`backend/storage.py` 的 JSON 文件存储。
- 当前后台任务：`backend/app.py` 进程内 asyncio task。
