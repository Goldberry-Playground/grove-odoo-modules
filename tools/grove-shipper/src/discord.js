// One Discord summary message per run to DISCORD_ORDERS_WEBHOOK_URL. Best-effort:
// a webhook failure is logged but never masks the run's own exit status.

/**
 * @param {string|undefined} webhookUrl
 * @param {object} summary
 * @param {string} summary.batchName
 * @param {number} summary.rows
 * @param {number} summary.total
 * @param {string} [summary.batchUrl]
 * @param {string} [summary.mode]      'dry-run' | 'bought'
 * @param {string[]} [summary.problems]
 * @returns {Promise<boolean>} true if posted
 */
export async function postDiscordSummary(webhookUrl, summary) {
  if (!webhookUrl) {
    console.warn('[discord] DISCORD_ORDERS_WEBHOOK_URL not set — skipping summary');
    return false;
  }
  const { batchName, rows, total, batchUrl, mode = 'dry-run', problems = [] } = summary;
  const ok = problems.length === 0;
  const head = ok
    ? `📦 **Pirate Ship ${mode === 'bought' ? 'labels purchased' : 'dry-run'}** — ${batchName}`
    : `⚠️ **Pirate Ship run had problems** — ${batchName}`;
  const lines = [
    head,
    `Rows: **${rows}**  •  Total: **$${Number(total).toFixed(2)}**`,
  ];
  if (batchUrl) lines.push(`Batch: ${batchUrl}`);
  if (problems.length) {
    lines.push('', 'Problems:');
    for (const p of problems.slice(0, 20)) lines.push(`• ${p}`);
    if (problems.length > 20) lines.push(`• …and ${problems.length - 20} more`);
  }
  const content = lines.join('\n').slice(0, 1900);
  try {
    const res = await fetch(webhookUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    if (!res.ok) {
      console.warn(`[discord] webhook returned HTTP ${res.status}`);
      return false;
    }
    return true;
  } catch (err) {
    console.warn(`[discord] post failed: ${err.message}`);
    return false;
  }
}
