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
