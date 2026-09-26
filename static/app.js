const $ = id => document.getElementById(id);
let activeId = null, activeReport = null, busy = false, maxMB = 20;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const element = (tag, text, className) => { const e = document.createElement(tag); e.textContent = text; if (className) e.className = className; return e; };
function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; }
function setBusy(value) { busy = value; $('submit').disabled = value; $('file').disabled = value; $('progress').hidden = !value; }
function selectFile() { const file = $('file').files[0]; $('file-label').textContent = file ? file.name : 'Choose a PDF or drop it here'; }
$('file').addEventListener('change', selectFile);
for (const type of ['dragenter', 'dragover']) $('dropzone').addEventListener(type, e => {e.preventDefault(); if (!busy) $('dropzone').classList.add('dragging');});
for (const type of ['dragleave', 'drop']) $('dropzone').addEventListener(type, e => { e.preventDefault(); $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', e => { if (busy) return; if (e.dataTransfer.files.length !== 1) return notice('Choose one PDF at a time.'); $('file').files = e.dataTransfer.files; selectFile(); });
function statusMessage(status) {
  if (status === 413) return `The PDF is too large for the server (limit ${maxMB} MB).`;
  if (status === 404) return 'Review not found or expired. Upload the PDF again.';
  if (status === 429) return 'Too many requests. Wait a moment and retry.';
  if (status >= 500) return 'The server is temporarily unavailable. Please retry shortly.';
  return `The request could not be completed (HTTP ${status}).`;
}
async function request(url, options) {
  let response;
  try { response = await fetch(url, options); }
  catch { throw Error('Cannot reach the server. Check your connection and retry.'); }
  let data = null;
  try { data = await response.json(); } catch {}  // Proxies can return HTML error pages.
  if (!response.ok) throw Error(typeof data?.detail === 'string' ? data.detail : statusMessage(response.status));
  if (data === null) throw Error('The server returned an unreadable response. Please retry.');
  return data;
}
request('/healthz').then(data=>{
  if (Number.isFinite(data.max_upload_mb) && data.max_upload_mb > 0) maxMB = data.max_upload_mb;
  $('file-help').textContent = `One file · up to ${maxMB} MB`;
  if (!data.parser_configured) notice('LlamaParse is not configured yet. Add a LlamaParse API key to the server environment to enable reviews.');
  else if (data.config_errors?.length) notice('Server configuration error: ' + data.config_errors.join(' '));
}).catch(error=>notice(`${error.message} Refresh to retry.`));
$('upload-form').addEventListener('submit', async e => {
  e.preventDefault(); if (busy) return;
  const file = $('file').files[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf')) return notice('Choose one PDF file.');
  if (file.size > maxMB*1024*1024) return notice(`The PDF must be ${maxMB} MB or smaller.`);
  notice(''); $('results').hidden = true; setBusy(true); $('stage').textContent = 'Uploading and checking your PDF';
  const body = new FormData(); body.append('file', file);
  try {
    const created = await request('/api/reviews', {method:'POST', body}); activeId = created.id;
    let failures = 0;
    for (;;) {
      await sleep(2000);
      let job;
      try { job = await request(created.status_url); failures = 0; }
      catch (error) { if (++failures >= 5) throw error; $('stage').textContent = 'Reconnecting to your review…'; continue; }
      $('stage').textContent = job.stage;
      if (job.status === 'failed') throw Error(job.error || 'The review could not complete. Please retry.');
      if (job.status === 'completed') { showReport(job); break; }
    }
  } catch (error) {notice(error.message);}
  finally {setBusy(false);}
});
function preview(page) { $('page-select').value = page; $('preview').src = `/api/reviews/${activeId}/pages/${page}`; $('preview').alt = `Original uploaded form, page ${page}`; }
$('page-select').addEventListener('change', ()=>preview($('page-select').value));
$('preview').addEventListener('error', ()=>notice('The page preview could not load. The review may have expired; upload the PDF again.'));
$('filter').addEventListener('change', showIssues);
function showReport(job) {
  activeReport = job.report;
  $('filename').textContent = job.filename;
  $('form-type').textContent = activeReport.form_type;
  $('result-title').textContent = {passed:'All checks passed',issues_found:'Corrections needed',needs_review:'Manual verification needed',unsupported:'Form not recognized'}[activeReport.status];
  $('result-description').textContent = activeReport.summary;
  $('summary').className = `summary ${activeReport.status}`;
  $('issue-count').textContent = activeReport.issues.length;
  $('checks-count').textContent = activeReport.checks_run;
  $('download').href = `/api/reviews/${activeId}/report`;
  $('page-select').replaceChildren();
  for (let page=1;page<=activeReport.page_count;page++){const option = element('option', `${page} / ${activeReport.page_count}`); option.value = page; $('page-select').append(option);}
  $('filter').value = 'all'; showIssues(); showPassedChecks(); showExtracted(); preview(1);
  $('results').hidden = false; $('results-title').focus({preventScroll:true}); $('results').scrollIntoView({behavior:'smooth',block:'start'});
}
function showIssues() {
  $('issue-list').replaceChildren();
  const filter = $('filter').value;
  const issues = activeReport.issues.filter(i=>filter==='all'||i.severity===filter);
  if (!issues.length) $('issue-list').append(element('p', activeReport.status==='passed' ? 'No issues found in the applicable challenge checks. Continue with staff review.' : 'No findings in this category.', 'empty'));
  for (const issue of issues) {
    const card = element('article','',`issue ${issue.severity}`);
    const head = element('div','','issue-head'); head.append(element('span',issue.severity==='error'?'Field issue':'Manual verification','issue-badge'));
    if (issue.page) {const button = element('button',`Page ${issue.page} ↗`,'page-link');button.type='button';button.addEventListener('click',()=>{preview(issue.page); if(window.innerWidth<850) $('preview').scrollIntoView({behavior:'smooth'});});head.append(button);}
    card.append(head,element('h4',[issue.section,issue.row,issue.field].filter(Boolean).join(' · ')),element('p',issue.message));
    if (issue.observed !== null) card.append(element('p',`Read as: ${issue.observed || '(blank)'}`,'observed'));
    $('issue-list').append(card);
  }
}
function showPassedChecks() {
  const checks = activeReport.passed_checks || [];
  $('passed-checks').open = false;
  $('passed-count').textContent = checks.length;
  $('passed-list').replaceChildren();
  if (!checks.length) $('passed-list').append(element('p', 'No passed checks were recorded for this document.', 'empty'));
  for (const check of checks) {
    const card = element('article', '', 'issue passed-check');
    const head = element('div', '', 'issue-head');
    head.append(element('span', 'Passed', 'issue-badge'));
    const button = element('button', `Page ${check.page} ↗`, 'page-link');
    button.type = 'button';
    button.addEventListener('click', () => {
      preview(check.page);
      if (window.innerWidth < 850) $('preview').scrollIntoView({behavior:'smooth'});
    });
    head.append(button);
    card.append(head,
      element('h4', [check.section, check.row, check.field].filter(Boolean).join(' · ')),
      element('p', check.message),
      element('p', `Read as: ${check.observed || '(blank)'}`, 'observed'));
    $('passed-list').append(card);
  }
}
function showExtracted() {
  $('extracted').replaceChildren();
  for (const page of activeReport.extracted_pages) for (const section of page.extraction.sections) {
    $('extracted').append(element('h4',`Page ${page.page} · ${section.key.replaceAll('_',' ')}`));
    const table = element('table',''); const head=element('tr',''); for(const text of ['Row','Field','Transcription']) head.append(element('th',text)); table.append(head);
    for (const row of section.rows) for (const [field,cell] of Object.entries(row.cells)) {const tr=element('tr','');tr.append(element('td',row.label),element('td',field.replaceAll('_',' ')),element('td',cell.text || `(${cell.state})`));table.append(tr);}
    $('extracted').append(table);
  }
}
