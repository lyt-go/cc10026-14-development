<template>
  <section class="page recompute-page">
    <header class="page-head">
      <div>
        <h2>SLA 分类重算</h2>
        <p class="page-desc">
          对历史 SLA 事件明细批量重算错误率与事件量：按事件原时间窗口选取分类版本，
          多批采集覆盖同一请求只计一次，交付报告可逐条核对。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-all" type="button" @click="reloadAll">刷新</button>
      </div>
    </header>

    <nav class="tab-row">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        :data-testid="`tab-${tab.key}`"
        type="button"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>
    <p v-if="flashMessage" class="flash-text" data-testid="flash">{{ flashMessage }}</p>

    <!-- 版本时间线 -->
    <template v-if="activeTab === 'versions'">
      <div class="panel">
        <h3>分类版本生效时间线</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>版本</th><th>说明</th><th>生效起</th><th>生效止</th><th>规则数</th><th>错误码规则</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="version in versions" :key="version.version" :data-testid="`version-${version.version}`">
              <td><strong>{{ version.version }}</strong></td>
              <td>{{ version.label }}</td>
              <td>{{ version.effective_from }}</td>
              <td>{{ version.effective_to || '持续生效' }}</td>
              <td>{{ version.rule_count }}</td>
              <td class="rule-cell">
                <span v-for="rule in version.rules" :key="rule.type_code" class="chip">
                  {{ rule.code_range }} → {{ rule.type_name }}
                </span>
              </td>
            </tr>
            <tr v-if="!versions.length">
              <td colspan="6" class="empty-state">暂无分类版本</td>
            </tr>
          </tbody>
        </table>
        <p class="hint">
          重算以事件的 <code>window_start</code> 落在哪个版本生效区间为准；
          采集时间晚于生效日期也不会改变归类版本。
        </p>
      </div>

      <div class="panel">
        <h3>登记新分类版本</h3>
        <p class="hint">
          登记后到“重算操作台”对已有批次点“复算”，即可比较版本更新前后的异常量与归类口径。
          规则用 JSON 数组填写，每项包含 type_code / type_name / code_range。
        </p>
        <form class="run-form" @submit.prevent="submitVersion">
          <label>
            <span>版本号</span>
            <input v-model="versionForm.version" data-testid="version-no" placeholder="v2.1" required />
          </label>
          <label>
            <span>说明</span>
            <input v-model="versionForm.label" data-testid="version-label" placeholder="502 归入上游异常" />
          </label>
          <label>
            <span>生效起</span>
            <input v-model="versionForm.effectiveFrom" data-testid="version-from" placeholder="2026-10-03T00:00:00+08:00" required />
          </label>
          <label>
            <span>生效止（留空持续生效）</span>
            <input v-model="versionForm.effectiveTo" data-testid="version-to" placeholder="" />
          </label>
          <label class="rules-field">
            <span>规则 JSON</span>
            <textarea v-model="versionForm.rules" data-testid="version-rules" rows="4"></textarea>
          </label>
          <button class="btn primary" data-testid="submit-version" type="submit">登记版本</button>
        </form>
      </div>
    </template>

    <!-- 重算操作台 -->
    <template v-if="activeTab === 'runs'">
      <div class="panel">
        <h3>发起分类重算</h3>
        <form class="run-form" @submit.prevent="submitRun">
          <label>
            <span>查询窗口开始</span>
            <input v-model="form.windowStart" data-testid="window-start" placeholder="2026-09-28T00:00:00+08:00" required />
          </label>
          <label>
            <span>查询窗口结束</span>
            <input v-model="form.windowEnd" data-testid="window-end" placeholder="2026-10-05T00:00:00+08:00" required />
          </label>
          <label>
            <span>标题</span>
            <input v-model="form.title" data-testid="run-title" placeholder="可留空自动生成" />
          </label>
          <label>
            <span>分类口径</span>
            <select v-model="form.version" data-testid="run-version">
              <option value="">按事件原时间窗口自动选版</option>
              <option v-for="version in versions" :key="version.version" :value="version.version">
                锁定 {{ version.version }}
              </option>
            </select>
          </label>
          <label class="inline-check">
            <input v-model="form.dedupe" data-testid="run-dedupe" type="checkbox" />
            <span>同一请求多批采集只计一次</span>
          </label>
          <button class="btn primary" data-testid="submit-run" type="submit" :disabled="running">
            {{ running ? '重算中…' : '发起重算' }}
          </button>
        </form>
      </div>

      <div class="panel">
        <h3>重算记录</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>批次</th><th>标题</th><th>查询窗口</th><th>采集/去重</th>
              <th>错误率</th><th>异常量</th><th>版本</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="run in runs"
              :key="run.run_id"
              :class="{ selected: selectedRun?.run_id === run.run_id }"
              :data-testid="`run-${run.run_id}`"
            >
              <td>#{{ run.run_id }}</td>
              <td>{{ run.title }}</td>
              <td class="nowrap">{{ shortTime(run.window_start) }} ~ {{ shortTime(run.window_end) }}</td>
              <td>{{ run.raw_records }} / {{ run.unique_requests }} <span class="muted">去重{{ run.duplicates_dropped }}</span></td>
              <td>{{ run.error_rate_pct }}%</td>
              <td>{{ run.error_events }}</td>
              <td>
                <span class="chip">{{ run.classification_policy === 'pinned' ? '锁定' : '按窗口' }}</span>
                <span v-for="v in run.versions_used" :key="v" class="chip">{{ v }}</span>
              </td>
              <td class="row-actions">
                <button class="link" type="button" :data-testid="`select-run-${run.run_id}`" @click="selectRun(run.run_id)">明细</button>
                <button class="link" type="button" @click="rerun(run.run_id)">复算</button>
                <button class="link" type="button" @click="download(run.run_id)">导出</button>
                <button class="link" type="button" :data-testid="`deliver-${run.run_id}`" @click="deliver(run.run_id)">交付</button>
                <button class="link" type="button" @click="consume(run.run_id)">监控消费</button>
              </td>
            </tr>
            <tr v-if="!runs.length">
              <td colspan="8" class="empty-state">暂无重算批次，先在上方发起一次重算</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="selectedRun" class="panel" data-testid="run-detail">
        <h3>批次 #{{ selectedRun.run_id }} 复算明细</h3>
        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">请求量（去重后 / 原始）</span>
            <strong class="stat-value">{{ selectedRun.stats.total_requests }} / {{ selectedRun.raw_records }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">异常事件</span>
            <strong class="stat-value">{{ selectedRun.stats.error_events }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">窗口错误率</span>
            <strong class="stat-value">{{ selectedRun.stats.error_rate_pct }}%</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">重复采集合并</span>
            <strong class="stat-value">{{ selectedRun.duplicates_dropped }}</strong>
          </article>
        </div>

        <h4>分类版本使用情况</h4>
        <table class="data-table">
          <thead>
            <tr><th>版本</th><th>事件数</th><th>异常数</th><th>最早事件</th><th>最晚事件</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in selectedRun.stats.versions_used" :key="row.version">
              <td>{{ row.version }}</td>
              <td>{{ row.event_count }}</td>
              <td>{{ row.error_count }}</td>
              <td>{{ row.first_event_at }}</td>
              <td>{{ row.last_event_at }}</td>
            </tr>
          </tbody>
        </table>

        <h4>异常量变化分解</h4>
        <div class="decompose" data-testid="decompose">
          <div class="decompose-row">
            <span>原始采集（统一口径）</span>
            <strong>{{ selectedRun.decomposition.raw_stats.total_requests }} 请求 / {{ selectedRun.decomposition.raw_stats.error_events }} 异常</strong>
            <em>{{ selectedRun.decomposition.raw_stats.error_rate_pct }}%</em>
          </div>
          <div class="decompose-row volume">
            <span>① 去重影响</span>
            <strong>{{ signed(selectedRun.decomposition.volume_effect.total_requests) }} 请求 / {{ signed(selectedRun.decomposition.volume_effect.error_events) }} 异常</strong>
            <em>{{ signed(selectedRun.decomposition.volume_effect.error_rate_pct, 'pt') }}</em>
          </div>
          <div class="decompose-row classification">
            <span>② 分类口径影响</span>
            <strong>{{ signed(selectedRun.decomposition.classification_effect.total_requests) }} 请求 / {{ signed(selectedRun.decomposition.classification_effect.error_events) }} 异常</strong>
            <em>{{ signed(selectedRun.decomposition.classification_effect.error_rate_pct, 'pt') }}</em>
          </div>
          <div class="decompose-row result">
            <span>重算结果</span>
            <strong>{{ selectedRun.decomposition.actual_stats.total_requests }} 请求 / {{ selectedRun.decomposition.actual_stats.error_events }} 异常</strong>
            <em>{{ selectedRun.decomposition.actual_stats.error_rate_pct }}%</em>
          </div>
        </div>
        <div v-if="selectedRun.decomposition.classification_effect.by_type.length" class="delta-list">
          <span class="muted">口径影响按类型：</span>
          <span v-for="row in selectedRun.decomposition.classification_effect.by_type" :key="row.type_name" class="chip">
            {{ row.type_name }} {{ signed(row.delta) }}
          </span>
        </div>

        <template v-if="selectedRun.comparison">
          <h4>与上次重算的对比（批次 #{{ selectedRun.comparison.before_run_id }} → #{{ selectedRun.comparison.after_run_id }}）</h4>
          <div class="decompose">
            <div class="decompose-row">
              <span>上次重算</span>
              <strong>{{ selectedRun.comparison.before.total_requests }} 请求 / {{ selectedRun.comparison.before.error_events }} 异常</strong>
              <em>{{ selectedRun.comparison.before.error_rate_pct }}%</em>
            </div>
            <div class="decompose-row volume">
              <span>① 请求量/去重变化</span>
              <strong>{{ signed(selectedRun.comparison.volume_effect.total_requests) }} 请求 / {{ signed(selectedRun.comparison.volume_effect.error_events) }} 异常</strong>
              <em>{{ signed(selectedRun.comparison.volume_effect.error_rate_pct, 'pt') }}</em>
            </div>
            <div class="decompose-row classification">
              <span>② 归类口径变化</span>
              <strong>{{ signed(selectedRun.comparison.classification_effect.total_requests) }} 请求 / {{ signed(selectedRun.comparison.classification_effect.error_events) }} 异常</strong>
              <em>{{ signed(selectedRun.comparison.classification_effect.error_rate_pct, 'pt') }}</em>
            </div>
            <div class="decompose-row result">
              <span>本次重算</span>
              <strong>{{ selectedRun.comparison.after.total_requests }} 请求 / {{ selectedRun.comparison.after.error_events }} 异常</strong>
              <em>{{ selectedRun.comparison.after.error_rate_pct }}%</em>
            </div>
          </div>
          <div v-if="selectedRun.comparison.classification_effect.by_type.length" class="delta-list">
            <span class="muted">口径变化按类型：</span>
            <span v-for="row in selectedRun.comparison.classification_effect.by_type" :key="row.type_name" class="chip">
              {{ row.type_name }} {{ signed(row.delta) }}
            </span>
          </div>
        </template>

        <h4>重复采集记录</h4>
        <table v-if="selectedRun.duplicate_groups.length" class="data-table">
          <thead>
            <tr><th>请求</th><th>保留事件</th><th>保留批次</th><th>被合并事件</th><th>原批次</th><th>事件时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="group in selectedRun.duplicate_groups" :key="group.request_id">
              <td>{{ group.request_id }}</td>
              <td>{{ group.kept_event_id }}</td>
              <td>{{ group.kept_batch }}</td>
              <td>{{ (group.dropped_event_ids || []).join(', ') }}</td>
              <td>{{ (group.dropped_batches || []).join(', ') }}</td>
              <td>{{ group.occurred_at }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="hint">该批次没有重复采集记录。</p>

        <h4>SLA 指标明细（可下钻事件）</h4>
        <table class="data-table">
          <thead>
            <tr><th>指标</th><th>异常类型</th><th>事件量</th><th>错误率</th><th>占比</th><th>版本分布</th><th>关联事件</th></tr>
          </thead>
          <tbody>
            <tr v-for="metric in selectedRun.metric_detail" :key="metric.metric_id" :data-testid="`metric-${metric.metric_id}`">
              <td>{{ metric.metric_id }}</td>
              <td>{{ metric.type_name }}</td>
              <td>{{ metric.count }}</td>
              <td>{{ metric.error_rate_pct }}%</td>
              <td>{{ ((metric.share_of_errors || 0) * 100).toFixed(1) }}%</td>
              <td>
                <span v-for="(count, version) in metric.versions" :key="version" class="chip">{{ version }}:{{ count }}</span>
              </td>
              <td class="event-links">
                <button
                  v-for="link in metric.event_links"
                  :key="link.event_id"
                  class="link"
                  type="button"
                  @click="focusEvent(link.event_id)"
                >
                  {{ link.event_id }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>

        <h4>逐事件明细</h4>
        <p class="hint">每条事件都记录实际采用的分类版本与原时间窗口；被合并的重复采集事件在“合并事件”列留痕。</p>
        <table class="data-table event-table">
          <thead>
            <tr>
              <th>事件</th><th>请求</th><th>API</th><th>响应码</th><th>事件时间</th>
              <th>原时间窗口</th><th>分类版本</th><th>归类</th><th>采集批次</th><th>合并事件</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in selectedRun.event_details"
              :key="item.event_id"
              :class="{ 'focus-row': focusedEvent === item.event_id }"
              :data-testid="`event-${item.event_id}`"
            >
              <td>{{ item.event_id }}</td>
              <td>{{ item.request_id }}</td>
              <td>{{ item.api }}</td>
              <td :class="{ 'code-error': item.is_error }">{{ item.response_code }}</td>
              <td class="nowrap">{{ item.occurred_at }}</td>
              <td class="nowrap">{{ item.window_start }} ~ {{ item.window_end }}</td>
              <td>{{ item.classification_version }}</td>
              <td>{{ item.type_name }}</td>
              <td>{{ item.collect_batch }}</td>
              <td>{{ (item.duplicate_event_ids || []).join(', ') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 交付报告核对 -->
    <template v-if="activeTab === 'reports'">
      <div class="panel">
        <h3>已交付报告（内容冻结）</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>报告</th><th>查询窗口</th><th>交付时间</th><th>分类版本</th>
              <th>去重</th><th>请求量</th><th>异常量</th><th>错误率</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="report in reports" :key="report.report_id" :data-testid="`report-${report.report_id}`">
              <td>{{ report.report_id }}<br /><span class="muted">{{ report.title }}</span></td>
              <td class="nowrap">{{ shortTime(report.window_start) }} ~ {{ shortTime(report.window_end) }}</td>
              <td class="nowrap">{{ report.delivered_at }}</td>
              <td>{{ report.classification_version }}</td>
              <td>{{ report.dedupe_applied ? '是' : '否' }}</td>
              <td>{{ report.total_requests }}</td>
              <td>{{ report.error_events }}</td>
              <td>{{ report.error_rate_pct }}%</td>
              <td>
                <button class="link" type="button" :data-testid="`reconcile-${report.report_id}`" @click="reconcile(report.report_id)">
                  核对
                </button>
              </td>
            </tr>
            <tr v-if="!reports.length">
              <td colspan="9" class="empty-state">暂无已交付报告</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="reconcileResult" class="panel" data-testid="reconcile-result">
        <h3>核对结果：{{ reconcileResult.report.report_id }}</h3>
        <p>
          <span class="badge" :class="reconcileResult.reproduce.matched ? 'ok' : 'warn'">
            {{ reconcileResult.reproduce.matched ? '历史报告可按原口径复现' : '历史报告无法按原口径完全复现' }}
          </span>
          <span class="muted">
            报告交付时锁定 {{ reconcileResult.report.classification_version }}，
            {{ reconcileResult.report.dedupe_applied ? '已去重' : '未去重（按原始采集记录统计）' }}。
          </span>
        </p>

        <h4>报告 vs 重算</h4>
        <table class="data-table">
          <thead>
            <tr><th>指标</th><th>交付报告</th><th>按原口径复现</th><th>重算结果</th><th>复现一致</th></tr>
          </thead>
          <tbody>
            <tr v-for="check in reconcileResult.reproduce.checks" :key="check.field">
              <td>{{ check.field }}</td>
              <td>{{ check.delivered }}</td>
              <td>{{ check.reproduced }}</td>
              <td>{{ runStat(check.field) }}</td>
              <td>{{ check.matched ? '✓' : '✗' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="reconcileResult.reproduce.missing_event_ids.length" class="error-text">
          报告引用的原始采集记录缺失：{{ reconcileResult.reproduce.missing_event_ids.join(', ') }}
        </p>

        <h4>差异归因（去重 vs 口径）</h4>
        <div class="effect-grid">
          <article class="effect-card">
            <span class="stat-label">采集去重影响</span>
            <strong>{{ signed(reconcileResult.effects.dedupe_effect.total_requests) }} 请求</strong>
            <span>{{ signed(reconcileResult.effects.dedupe_effect.error_events) }} 异常 / {{ signed(reconcileResult.effects.dedupe_effect.error_rate_pct, 'pt') }}</span>
          </article>
          <article class="effect-card">
            <span class="stat-label">分类口径影响</span>
            <strong v-if="reconcileResult.effects.classification_effect">
              {{ signed(reconcileResult.effects.classification_effect.total_requests) }} 请求
            </strong>
            <strong v-else>—</strong>
            <span v-if="reconcileResult.effects.classification_effect">
              {{ signed(reconcileResult.effects.classification_effect.error_events) }} 异常 /
              {{ signed(reconcileResult.effects.classification_effect.error_rate_pct, 'pt') }}
            </span>
            <span v-else class="muted">请选择对应重算批次后查看</span>
          </article>
          <article class="effect-card">
            <span class="stat-label">总差异</span>
            <strong v-if="reconcileResult.effects.total_delta">
              {{ signed(reconcileResult.effects.total_delta.total_requests) }} 请求
            </strong>
            <strong v-else>—</strong>
            <span v-if="reconcileResult.effects.total_delta">
              {{ signed(reconcileResult.effects.total_delta.error_events) }} 异常 /
              {{ signed(reconcileResult.effects.total_delta.error_rate_pct, 'pt') }}
            </span>
          </article>
        </div>

        <h4>运营可查的原因</h4>
        <div v-for="reason in reconcileResult.reasons" :key="reason.code" class="reason-card" :data-testid="`reason-${reason.code}`">
          <strong>{{ reason.title }}</strong>
          <p>{{ reason.detail }}</p>
          <ul>
            <li v-for="(item, index) in reason.evidence" :key="index">
              <code>{{ compact(item) }}</code>
            </li>
          </ul>
        </div>
        <p v-if="!reconcileResult.reasons.length" class="hint">未发现需要解释的差异。</p>
      </div>
    </template>

    <!-- 监控消费 -->
    <template v-if="activeTab === 'consume'">
      <div class="panel">
        <h3>SLA 监控消费分类重算指标明细</h3>
        <form class="run-form" @submit.prevent="consumeById">
          <label>
            <span>重算批次</span>
            <select v-model="consumeRunId" data-testid="consume-run">
              <option :value="0">请选择批次</option>
              <option v-for="run in runs" :key="run.run_id" :value="run.run_id">
                #{{ run.run_id }} {{ run.title }}
              </option>
            </select>
          </label>
          <button class="btn primary" data-testid="submit-consume" type="submit">重新统计窗口</button>
        </form>
      </div>

      <div v-if="consumption" class="panel" data-testid="consumption-result">
        <h3>批次 #{{ consumption.run_id }} 重新统计结果</h3>
        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">窗口请求量</span>
            <strong class="stat-value">{{ consumption.restated.total_requests }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">窗口异常量</span>
            <strong class="stat-value">{{ consumption.restated.error_events }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">窗口错误率</span>
            <strong class="stat-value">{{ consumption.restated.error_rate_pct }}%</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">可关联事件 / 未关联</span>
            <strong class="stat-value">
              {{ consumption.linked_event_count }} / {{ consumption.unlinked_events.length }}
            </strong>
          </article>
        </div>
        <p class="hint">
          分桶周期 {{ consumption.period_minutes }} 分钟；分类口径
          {{ consumption.classification_policy === 'pinned' ? '锁定版本' : '按事件时间窗口' }}，
          使用版本 {{ consumption.versions_used.join(', ') }}。
        </p>

        <h4>按异常类型重新统计</h4>
        <table class="data-table">
          <thead>
            <tr><th>指标</th><th>异常类型</th><th>事件量</th><th>错误率</th><th>关联事件</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in consumption.restated.by_type" :key="row.metric_id">
              <td>{{ row.metric_id }}</td>
              <td>{{ row.type_name }}</td>
              <td>{{ row.count }}</td>
              <td>{{ row.error_rate_pct }}%</td>
              <td class="event-links">
                <span v-for="eventId in row.event_ids" :key="eventId" class="chip">{{ eventId }}</span>
              </td>
            </tr>
          </tbody>
        </table>

        <h4>窗口时间序列</h4>
        <table class="data-table">
          <thead>
            <tr><th>桶开始</th><th>桶结束</th><th>请求量</th><th>异常量</th><th>错误率</th><th>类型分布</th></tr>
          </thead>
          <tbody>
            <tr v-for="bucket in consumption.restated.window_series" :key="bucket.bucket_start">
              <td class="nowrap">{{ rocketTime(bucket.bucket_start) }}</td>
              <td class="nowrap">{{ rocketTime(bucket.bucket_end) }}</td>
              <td>{{ bucket.total_requests }}</td>
              <td>{{ bucket.error_events }}</td>
              <td>{{ bucket.error_rate_pct }}%</td>
              <td>
                <span v-for="(count, name) in bucket.by_type" :key="name" class="chip">{{ name }}:{{ count }}</span>
                <span v-if="!Object.keys(bucket.by_type).length" class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>

        <div v-if="consumption.unlinked_events.length">
          <h4>未关联事件</h4>
          <ul>
            <li v-for="item in consumption.unlinked_events" :key="item.event_id">
              {{ item.event_id }}：{{ item.reason }}
            </li>
          </ul>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  consumeRun,
  createRun,
  deliverRun,
  downloadRun,
  getRun,
  listReports,
  listRuns,
  listVersions,
  reconcileReport,
  registerVersion,
  rerunRun,
} from '@/api/sla-recompute'
import type {
  ClassificationVersion,
  ConsumptionResult,
  RecomputeRun,
  ReconcileResult,
  ReportSummary,
  RunSummary,
} from '@/api/sla-recompute'

const tabs = [
  { key: 'versions', label: '分类版本' },
  { key: 'runs', label: '重算操作台' },
  { key: 'reports', label: '交付报告核对' },
  { key: 'consume', label: '监控消费' },
]

const activeTab = ref('runs')
const versions = ref<ClassificationVersion[]>([])
const runs = ref<RunSummary[]>([])
const reports = ref<ReportSummary[]>([])
const selectedRun = ref<RecomputeRun | null>(null)
const reconcileResult = ref<ReconcileResult | null>(null)
const consumption = ref<ConsumptionResult | null>(null)
const consumeRunId = ref(0)
const focusedEvent = ref('')
const errorMessage = ref('')
const flashMessage = ref('')
const running = ref(false)

const form = ref({
  windowStart: '2026-09-28T00:00:00+08:00',
  windowEnd: '2026-10-05T00:00:00+08:00',
  title: '',
  version: '',
  dedupe: true,
})

const versionForm = ref({
  version: '',
  label: '',
  effectiveFrom: '',
  effectiveTo: '',
  rules: '',
})

function fail(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
  flashMessage.value = ''
}

function flash(message: string) {
  flashMessage.value = message
  errorMessage.value = ''
}

function shortTime(value?: string | null) {
  if (!value) return '—'
  return value.replace('+08:00', '').replace('T', ' ').slice(0, 16)
}

function rocketTime(value?: string | null) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  const offset = parsed.getTime() + 8 * 3600 * 1000
  return new Date(offset).toISOString().replace('T', ' ').slice(0, 16)
}

function signed(value?: number, suffix = '') {
  if (value === undefined || value === null) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value}${suffix}`
}

function compact(item: Record<string, unknown>) {
  return Object.entries(item)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join('|') : String(value)}`)
    .join('  ')
}

function runStat(field: string): string {
  const stats = reconcileResult.value?.run?.stats
  if (!stats) return '—'
  if (field.startsWith('by_type.')) {
    const name = field.slice('by_type.'.length)
    return String(stats.by_type.find((row) => row.type_name === name)?.count ?? 0)
  }
  return String((stats as unknown as Record<string, number>)[field] ?? '—')
}

async function reloadAll() {
  errorMessage.value = ''
  try {
    const [versionRows, runRows, reportRows] = await Promise.all([
      listVersions(),
      listRuns(),
      listReports(),
    ])
    versions.value = versionRows
    runs.value = runRows
    reports.value = reportRows
  } catch (error) {
    fail(error, '重算数据读取失败')
  }
}

async function submitRun() {
  errorMessage.value = ''
  flashMessage.value = ''
  if (!form.value.windowStart || !form.value.windowEnd) {
    fail(new Error('查询窗口起止时间不能为空'), '')
    return
  }
  running.value = true
  try {
    const result = await createRun({
      window_start: form.value.windowStart,
      window_end: form.value.windowEnd,
      title: form.value.title || undefined,
      dedupe: form.value.dedupe,
      classification_version: form.value.version || null,
    })
    if (!result.ok || !result.run) throw new Error(result.message)
    flash(result.message)
    selectedRun.value = result.run
    await reloadAll()
  } catch (error) {
    fail(error, '分类重算失败')
  } finally {
    running.value = false
  }
}

async function submitVersion() {
  errorMessage.value = ''
  flashMessage.value = ''
  let rules: unknown
  try {
    rules = JSON.parse(versionForm.value.rules || '[]')
  } catch {
    fail(new Error('规则 JSON 解析失败，请检查格式'), '')
    return
  }
  if (!Array.isArray(rules) || !rules.length) {
    fail(new Error('规则 JSON 不能为空数组'), '')
    return
  }
  try {
    const result = await registerVersion({
      version: versionForm.value.version,
      label: versionForm.value.label || undefined,
      effective_from: versionForm.value.effectiveFrom,
      effective_to: versionForm.value.effectiveTo || null,
      rules: rules as { type_name: string; code_range: string }[],
    })
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    versionForm.value = { version: '', label: '', effectiveFrom: '', effectiveTo: '', rules: '' }
    await reloadAll()
  } catch (error) {
    fail(error, '版本登记失败')
  }
}

async function selectRun(runId: number) {
  errorMessage.value = ''
  try {
    selectedRun.value = await getRun(runId)
    focusedEvent.value = ''
  } catch (error) {
    fail(error, '重算明细读取失败')
  }
}

async function rerun(runId: number) {
  errorMessage.value = ''
  try {
    const result = await rerunRun(runId)
    if (!result.ok || !result.run) throw new Error(result.message)
    flash(result.message)
    selectedRun.value = result.run
    await reloadAll()
  } catch (error) {
    fail(error, '复算失败')
  }
}

async function deliver(runId: number) {
  errorMessage.value = ''
  try {
    const result = await deliverRun(runId)
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    await reloadAll()
  } catch (error) {
    fail(error, '交付失败')
  }
}

async function download(runId: number) {
  errorMessage.value = ''
  try {
    await downloadRun(runId)
  } catch (error) {
    fail(error, '导出失败')
  }
}

async function consume(runId: number) {
  errorMessage.value = ''
  try {
    const result = await consumeRun(runId)
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    consumption.value = result.consumption
    consumeRunId.value = runId
    activeTab.value = 'consume'
  } catch (error) {
    fail(error, '监控消费失败')
  }
}

async function consumeById() {
  if (!consumeRunId.value) {
    fail(new Error('请选择重算批次'), '')
    return
  }
  await consume(consumeRunId.value)
}

async function reconcile(reportId: string) {
  errorMessage.value = ''
  try {
    reconcileResult.value = await reconcileReport(reportId, selectedRun.value?.run_id)
  } catch (error) {
    fail(error, '报告核对失败')
  }
}

function focusEvent(eventId?: string) {
  focusedEvent.value = eventId || ''
  const target = document.querySelector(`[data-testid="event-${eventId}"]`)
  target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

onMounted(reloadAll)
</script>

<style scoped>
.tab-row { display: flex; gap: 8px; margin-bottom: 12px; }
.tab { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 14px; cursor: pointer; }
.tab.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 14px; }
.panel h3 { margin: 0 0 10px; font-size: 15px; }
.panel h4 { margin: 16px 0 8px; font-size: 13px; color: #334155; }
.run-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.run-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.run-form input, .run-form select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.run-form .rules-field { flex: 1 1 100%; }
.run-form textarea { width: 100%; padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-family: monospace; font-size: 12px; }
.run-form .inline-check { flex-direction: row; align-items: center; gap: 6px; color: #1f2937; }
.rule-cell { max-width: 420px; }
.chip { display: inline-block; border: 1px solid var(--border); border-radius: 10px; padding: 1px 8px; margin: 2px 4px 2px 0; font-size: 11px; color: #334155; background: #f8fafc; }
.hint { color: var(--muted); font-size: 12px; }
.muted { color: var(--muted); }
.nowrap { white-space: nowrap; }
.flash-text { color: #067647; font-size: 13px; }
tr.selected td { background: #eef5ff; }
.decompose { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.decompose-row { display: grid; grid-template-columns: 200px 1fr 120px; gap: 8px; padding: 8px 12px; font-size: 13px; border-bottom: 1px solid #eef1f5; }
.decompose-row:last-child { border-bottom: none; }
.decompose-row em { font-style: normal; color: var(--muted); text-align: right; }
.decompose-row.volume { background: #fff8e6; }
.decompose-row.classification { background: #eef5ff; }
.decompose-row.result { background: #ecfdf3; font-weight: 600; }
.delta-list { margin-top: 8px; font-size: 12px; }
.event-table td { font-size: 12px; }
.event-links { display: flex; flex-wrap: wrap; gap: 6px; }
.code-error { color: #b42318; font-weight: 600; }
.focus-row td { background: #fff3d6; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 12px; margin-right: 8px; }
.badge.ok { background: #ecfdf3; color: #067647; }
.badge.warn { background: #fef3f2; color: #b42318; }
.effect-grid { display: flex; gap: 12px; flex-wrap: wrap; }
.effect-card { flex: 1; min-width: 200px; display: flex; flex-direction: column; gap: 4px; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; font-size: 12px; }
.effect-card strong { font-size: 16px; }
.reason-card { border-left: 3px solid var(--brand); padding: 8px 12px; background: #f8fafc; margin-bottom: 8px; }
.reason-card p { margin: 4px 0; font-size: 13px; }
.reason-card ul { margin: 0; padding-left: 18px; }
.reason-card code { font-size: 11px; color: #475569; }
</style>
