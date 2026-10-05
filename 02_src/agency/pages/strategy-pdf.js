import {esc} from '../components/ui.js';

// Use the browser's PDF printer so Arabic text keeps its font and shaping.
export async function downloadStrategyPdf(strategy) {
  const frame = document.createElement('iframe');
  frame.title = 'Strategy print preview';
  frame.style.cssText = 'position:fixed;width:0;height:0;border:0;';
  document.body.append(frame);
  const doc = frame.contentDocument;
  const list = (title, items) => items?.length
    ? `<section><h2>${title}</h2><ul>${items.map(item => `<li dir="auto">${esc(item)}</li>`).join('')}</ul></section>` : '';
  try {
    doc.open();
    doc.write(`<!doctype html><html><head><meta charset="utf-8">
      <title>${esc(strategy.restaurant_name)} - Rawaj Strategy</title>
      <style>
        @page { size: A4; margin: 18mm; }
        body { font: 12pt/1.6 Arial, sans-serif; color: #10213f; }
        h1 { font-size: 24pt; } h2 { font-size: 16pt; }
        h3 { font-size: 13pt; margin-bottom: 4px; }
        article { break-inside: avoid; border-top: 1px solid #ccc; padding: 12px 0; }
        p { white-space: pre-wrap; margin: 4px 0 12px; }
        small { color: #555; }
      </style></head><body>
      <h1 dir="auto">${esc(strategy.restaurant_name)}</h1>
      <p>Rawaj Strategy · ${esc(strategy.start_date)} – ${esc(strategy.end_date)}</p>
      <p dir="auto">${esc(strategy.summary || strategy.focus || '')}</p>
      ${list('Targets', strategy.targets)}
      ${list('Content pillars', strategy.pillars?.map(p => `${p.title}: ${p.description}`))}
      ${list('Marketing gaps', strategy.gaps?.map(g => `${g.gap} (${g.severity})`))}
      ${list('Recommended services', strategy.services?.map(s => `${s.service}: ${s.why_this_service_fits}`))}
      <h2>Action plan</h2>
      ${(strategy.days || []).map(day => `<article>
        <h3 dir="auto">Day ${esc(day.day)} · ${esc(day.focus || day.title || 'Planned activity')}</h3>
        <small>${esc(day.date || '')} · ${esc(day.status || 'Planned')}</small>
        <p dir="auto">${esc(day.action || day.description || day.instructions || '')}</p>
      </article>`).join('')}
      </body></html>`);
    doc.close();
    await doc.fonts.ready;
    const printWindow = frame.contentWindow;
    printWindow.addEventListener('afterprint', () => frame.remove(), {once: true});
    printWindow.focus();
    printWindow.print();
  } catch (error) {
    frame.remove();
    throw error;
  }
}
