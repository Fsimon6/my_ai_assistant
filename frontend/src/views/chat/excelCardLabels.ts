/**
 * 第 3 项「结果与可解释性」P2：卡片文案的**纯函数**（G1 / G2 / G5）。
 *
 * 原则：只做展示文案拼装；所有数值与口径 100% 来自后端（前端不计算、不推断、不补零）。
 * 兼容：旧历史快照缺少这些**可选增量字段**时优雅降级（返回空串或省略子句），不报错。
 */

/** G1：第 1 步的「匹配 Excel 行区间」（来自分组执行器逐行命中的真实行号）。 */
export function multiStepSpanLabel(step1: any): string {
  const span = step1?.row_excel_spans
  if (!span || typeof span.first !== 'number' || typeof span.last !== 'number') return ''
  const base = `匹配 Excel 行区间：${span.first} ~ ${span.last}`
  return step1?.row_excel_truncated ? `${base}（行号已按上限裁剪）` : base
}

/** G2：第 2 步输入说明。`numeric_rows` 缺失（旧快照）时省略该子句；为 0 时如实显示。 */
export function multiStepStep2InputLabel(step2: any): string {
  const input = step2?.input_rows
  if (typeof input !== 'number') return ''
  const numeric = step2?.numeric_rows
  if (typeof numeric !== 'number') return `（输入 ${input} 个分组）`
  return `（输入 ${input} 个分组，其中可数值化 ${numeric} 个）`
}

/** G5：条件放宽说明（与表格/单聚合卡片同一格式、同一措辞）。 */
export function groupRelaxedLabel(res: any): string {
  const notes = res?.relaxed_filters
  return Array.isArray(notes) && notes.length ? notes.join('；') : ''
}

/**
 * R1：表格卡片底部的行范围说明。
 *
 * 语义澄清（只改措辞，数值与分页逻辑完全不变）：`row_excel_numbers` 只覆盖
 * **当前页返回的行**，因此首/末行号必须写明是「**本页** Excel 行号」，避免与
 * 「共命中 N 行」并读时被误认为"全部命中的行号都在这段区间内"。
 *
 * P1（2026-09-29）：当后端仍有更多命中行（`has_more === true`）时**必须明说**
 * "结果超过单次显示上限"，避免用户误以为"数据库里只有这些"；继续获取沿用既有
 * 分页机制（对助手说「下一页」）。仅当 `has_more === true` 时追加，旧快照/无该字段时不显示。
 */
export function tableRowRangeLabel(r: any): string {
  const nums = r?.row_excel_numbers || []
  const offset = typeof r?.offset === 'number' ? r.offset : 0
  const returned = typeof r?.returned_count === 'number' ? r.returned_count : nums.length
  const total = typeof r?.total_matches === 'number' ? r.total_matches : 0
  const seqStart = offset + 1
  const seqEnd = offset + returned
  const capNotice = r?.has_more === true
    ? `｜结果超过单次显示上限（${r?.limit} 条），可继续说「下一页」继续获取`
    : ''
  if (!nums.length) {
    return `无匹配行（起点为第 ${seqStart} 条，共命中 ${total} 行）`
  }
  return `本页 Excel 行号 ${nums[0]} ~ ${nums[nums.length - 1]}｜共命中 ${total} 行，`
    + `本次返回 ${returned} 行（第 ${seqStart}~${seqEnd} 条，offset=${offset}, limit=${r?.limit}）`
    + capNotice
}

/**
 * 相邻 P2：表格卡片的「全部命中 Excel 行」说明（真实最大连续段，**不伪造连续**）。
 *
 * 数据来自后端 `matched_row_runs` / `matched_row_run_count` / `matched_row_runs_truncated`
 * （由执行器把"全部命中行"的物理行号折叠成最大连续段；本页行号另见 tableRowRangeLabel）：
 *   * 单段连续 -> `全部命中 Excel 行：3 ~ 449`
 *   * 多段不连续 -> `全部命中 Excel 行：3、12、18~19`（保留真实不连续性）
 *   * 段数被截断 -> `全部命中 Excel 行：共 447 个命中行，分布在 87 段｜范围 3 ~ 449`
 *   * 无 runs（旧快照 / 命中行未随本页返回）-> 空串（宁可不显示，也不猜）
 */
export function matchedRowsLabel(r: any): string {
  const runs = Array.isArray(r?.matched_row_runs) ? r.matched_row_runs : []
  if (!runs.length) return ''
  const fmt = (pair: any[]) => (pair[0] === pair[1] ? `${pair[0]}` : `${pair[0]}~${pair[1]}`)
  if (r?.matched_row_runs_truncated === true) {
    const seg = typeof r?.matched_row_run_count === 'number' ? r.matched_row_run_count : runs.length
    const total = typeof r?.total_matches === 'number' ? r.total_matches : ''
    const first = runs[0][0]
    const last = runs[runs.length - 1][1]
    return `全部命中 Excel 行：共 ${total} 个命中行，分布在 ${seg} 段｜范围 ${first} ~ ${last}`
  }
  if (runs.length === 1) {
    const [a, b] = runs[0]
    // 单段：与项目既有"匹配 Excel 行区间：3 ~ 21 / 本页 Excel 行号：3 ~ 7"同格式
    return a === b ? `全部命中 Excel 行：${a}` : `全部命中 Excel 行：${a} ~ ${b}`
  }
  return `全部命中 Excel 行：${runs.map(fmt).join('、')}`
}

/**
 * R4：第 2 步的数据来源说明（自然中文，动态带出**本步实际输入的分组数**）。
 *
 * 保留原语义（关键正确性声明）：第 2 步只对第 1 步产出的**聚合结果**再计算，
 * **不会**重新回到原始 Excel 数据行；因此不写死任何 N/TOP 文案。
 * 旧快照缺 `input_rows` 时退化为不含数量的说法（不报错）。
 */
export function multiStepStep2SourceLabel(step2: any): string {
  const input = step2?.input_rows
  const amount = typeof input === 'number' ? `第 1 步的结果（${input} 个分组的聚合值）` : '第 1 步的结果'
  return `数据来源：${amount}，不会重新回到原始 Excel 数据行`
}
