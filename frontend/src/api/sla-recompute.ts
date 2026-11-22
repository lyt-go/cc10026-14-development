/** SLA分类重算接口：版本时间线、批量重算、交付报告与核对。 */
import { request } from './client'

export type ClassificationRule = {
  type_code?: string
  type_name: string
  code_range: string
  severity?: string
  retry?: string
  breaker?: string
  notify?: string
}

export type ClassificationVersion = {
  version: string
  label?: string
  effective_from: string
  effective_to?: string | null
  rule_count?: number
  rules: ClassificationRule[]
}

export type TypeBucket = {
  type_code: string
  type_name: string
  count: number
  error_rate: number
  error_rate_pct: number
  share_of_errors: number
  versions?: Record<string, number>
  event_ids?: string[]
  request_ids?: string[]
  events?: EventSummary[]
}

export type EventSummary = {
  event_id?: string
  request_id?: string
  api?: string
  response_code?: string
  occurred_at?: string
  window_start?: string
  window_end?: string
  classification_version?: string
  type_name?: string
}

export type RecomputeStats = {
  total_requests: number
  success_events: number
  error_events: number
  classified_error_events: number
  unclassified_error_events: number
  error_rate: number
  error_rate_pct: number
  by_type: TypeBucket[]
  versions_used: {
    version: string
    event_count: number
    error_count: number
    first_event_at?: string
    last_event_at?: string
  }[]
}

export type StatSummary = {
  total_requests: number
  success_events: number
  error_events: number
  error_rate: number
  error_rate_pct: number
}

export type StatsDelta = {
  total_requests: number
  error_events: number
  error_rate_pct: number
  by_type: {
    type_name: string
    before: number
    after: number
    delta: number
  }[]
}

export type DuplicateGroup = {
  request_id: string
  kept_event_id?: string
  kept_batch?: string
  dropped_event_ids?: (string | undefined)[]
  dropped_batches?: (string | undefined)[]
  occurred_at?: string
}

export type EventDetail = {
  event_id?: string
  request_id?: string
  api?: string
  caller?: string
  response_code?: string
  is_error?: boolean
  occurred_at?: string
  window_start?: string
  window_end?: string
  collect_batch?: string
  collected_at?: string
  source_batches?: string[]
  duplicate_event_ids?: string[]
  raw_record_count?: number
  classification_version?: string
  type_code?: string
  type_name?: string
  severity?: string
  classified?: boolean
}

export type MetricDetail = {
  metric_id: string
  run_id: number
  type_code?: string
  type_name?: string
  count?: number
  error_rate?: number
  error_rate_pct?: number
  share_of_errors?: number
  versions?: Record<string, number>
  event_links: EventSummary[]
}

export type RecomputeRun = {
  run_id: number
  title: string
  created_at: string
  created_by: string
  note?: string
  window_start: string
  window_end: string
  dedupe: boolean
  classification_policy: 'window' | 'pinned'
  pinned_version?: string | null
  baseline_version?: string | null
  parent_run_id?: number | null
  status: string
  raw_records: number
  unique_requests: number
  duplicates_dropped: number
  source_record_count: number
  duplicate_groups: DuplicateGroup[]
  stats: RecomputeStats
  versions_snapshot: ClassificationVersion[]
  event_details?: EventDetail[]
  metric_detail: MetricDetail[]
  decomposition: {
    baseline_version: string | null
    raw_stats: StatSummary
    volume_stats: StatSummary
    actual_stats: StatSummary
    volume_effect: StatsDelta
    classification_effect: StatsDelta
    notes: string[]
  }
  comparison?: {
    before_run_id?: number
    after_run_id?: number
    before: StatSummary
    after: StatSummary
    total_delta: StatsDelta
    volume_effect: StatsDelta
    classification_effect: StatsDelta
    notes: string[]
  }
  delivered_report_id?: string
}

export type RunSummary = {
  run_id: number
  title: string
  created_at: string
  created_by: string
  window_start: string
  window_end: string
  dedupe: boolean
  classification_policy: string
  pinned_version?: string | null
  raw_records: number
  unique_requests: number
  duplicates_dropped: number
  error_events: number
  error_rate_pct: number
  versions_used: string[]
  parent_run_id?: number | null
  delivered_report_id?: string
}

export type ReportSummary = {
  report_id: string
  title: string
  window_start: string
  window_end: string
  delivered_at: string
  delivered_by: string
  classification_version: string
  dedupe_applied: boolean
  total_requests: number
  error_events: number
  error_rate_pct: number
}

export type ReconcileResult = {
  report: {
    report_id: string
    title: string
    delivered_at: string
    classification_version: string
    dedupe_applied: boolean
    stats: RecomputeStats
  }
  run: {
    run_id: number
    classification_policy: string
    dedupe: boolean
    stats: RecomputeStats
  } | null
  reproduce: {
    matched: boolean
    checks: {
      field: string
      delivered: number | null
      reproduced: number | null
      matched: boolean
    }[]
    missing_event_ids: string[]
    source_record_count: number
    stats: StatSummary
  }
  dedupe: {
    raw_records: number
    unique_requests: number
    duplicates_dropped: number
    duplicate_groups: DuplicateGroup[]
    stats: StatSummary
  }
  restated: {
    matches_delivered: boolean
    dedupe_applied: boolean
    stats: StatSummary
  }
  effects: {
    dedupe_effect: StatsDelta
    classification_effect: StatsDelta | null
    total_delta: StatsDelta | null
  }
  reasons: {
    code: string
    title: string
    detail: string
    evidence: Record<string, unknown>[]
  }[]
}

export type ConsumptionResult = {
  run_id: number
  title: string
  window_start: string
  window_end: string
  period_minutes: number
  classification_policy: string
  pinned_version?: string | null
  versions_used: string[]
  dedupe: boolean
  duplicates_dropped: number
  restated: {
    total_requests: number
    success_events: number
    error_events: number
    error_rate: number
    error_rate_pct: number
    by_type: {
      type_code?: string
      type_name?: string
      count?: number
      error_rate_pct?: number
      metric_id: string
      event_ids: (string | undefined)[]
    }[]
    window_series: {
      bucket_start: string
      bucket_end: string
      total_requests: number
      error_events: number
      error_rate_pct: number
      by_type: Record<string, number>
    }[]
  }
  metric_count: number
  linked_event_count: number
  unlinked_events: { event_id: string; reason: string }[]
  event_index: Record<
    string,
    {
      request_id?: string
      type_name?: string
      window_start?: string
      window_end?: string
      classification_version?: string
    }
  >
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export async function listVersions(): Promise<ClassificationVersion[]> {
  return (await asJson<{ items: ClassificationVersion[] }>(await request('/api/sla-recompute/versions'))).items
}

export async function registerVersion(payload: {
  version: string
  label?: string
  effective_from: string
  effective_to?: string | null
  rules: ClassificationRule[]
}): Promise<{ ok: boolean; message: string }> {
  return asJson(await request('/api/sla-recompute/versions', {
    method: 'POST',
    body: JSON.stringify(payload),
  }))
}

export async function listRuns(): Promise<RunSummary[]> {
  return (await asJson<{ items: RunSummary[] }>(await request('/api/sla-recompute/runs'))).items
}

export async function createRun(payload: {
  window_start: string
  window_end: string
  title?: string
  created_by?: string
  note?: string
  dedupe?: boolean
  classification_version?: string | null
}): Promise<{ ok: boolean; message: string; run?: RecomputeRun }> {
  return asJson(await request('/api/sla-recompute/runs', {
    method: 'POST',
    body: JSON.stringify(payload),
  }))
}

export async function getRun(runId: number): Promise<RecomputeRun> {
  return asJson(await request(`/api/sla-recompute/runs/${runId}`))
}

export async function rerunRun(runId: number): Promise<{ ok: boolean; message: string; run?: RecomputeRun }> {
  return asJson(await request(`/api/sla-recompute/runs/${runId}/rerun`, {
    method: 'POST',
    body: JSON.stringify({}),
  }))
}

export async function deliverRun(
  runId: number,
  payload: { report_id?: string; title?: string; note?: string } = {},
): Promise<{ ok: boolean; message: string; report?: Record<string, unknown> }> {
  return asJson(await request(`/api/sla-recompute/runs/${runId}/deliver`, {
    method: 'POST',
    body: JSON.stringify(payload),
  }))
}

export async function consumeRun(runId: number): Promise<{ ok: boolean; message: string; consumption: ConsumptionResult }> {
  return asJson(await request(`/api/sla-recompute/runs/${runId}/consume`, { method: 'POST' }))
}

export async function listReports(): Promise<ReportSummary[]> {
  return (await asJson<{ items: ReportSummary[] }>(await request('/api/sla-recompute/reports'))).items
}

export async function reconcileReport(reportId: string, runId?: number): Promise<ReconcileResult> {
  const query = new URLSearchParams({ report_id: reportId })
  if (runId) query.set('run_id', String(runId))
  return asJson(await request(`/api/sla-recompute/reconcile?${query.toString()}`))
}

export async function downloadRun(runId: number): Promise<void> {
  const response = await request(`/api/sla-recompute/runs/${runId}/export`)
  if (!response.ok) throw new Error('重算明细导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `sla_recompute_${String(runId).padStart(6, '0')}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}
