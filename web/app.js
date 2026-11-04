'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  book: null, books: [], catalog: { templates: [], universes: [], difficulties: [] },
  step: 1, pageId: null, variantId: null, view: 'solution_final', job: null,
  export: null, busy: false, metadataDirty: false, metadataSaving: null,
  metadataVersion: 0, proofIndex: 0, draggedPage: null, uploading: false,
  pendingPhotos: [], photoUniverses: new Map(), choiceSequences: new Map(),
  queuedGeneration: null, pinnedGenerations: new Map(), favoritesOnly: false, favoriteBusy: new Set(),
  favoriteIntents: new Map(), localPhotos: new Map(), bookEpoch: 0, assetUrlsAt: 0, failedChoices: new Set(),
  universeTab: 'artistic', universeNotice: '',
};
let saveTimer, generationTimer, elapsedTimer;
let choiceSequence = 0, mutationEpoch = 0, bookOpenSequence = 0, writeTail = Promise.resolve();
const responseEpochs = new WeakMap(), previewBundles = new Map(), localObjectUrls = new Set();
const stageNames = ['LE LIVRE', 'LES SOUVENIRS', 'LA COMPOSITION', 'LE CADEAU'];
const difficultyNames = { relaxed: 'Détente', balanced: 'Équilibré', expert: 'Minutieux' };
const maskingNames = { guided: 'Dessin visible', discreet: 'Surprise douce', mystery: 'Mystère renforcé' };
const viewNames = { solution_final: 'APERÇU COLORIÉ', mystery: 'PAGE À COLORIER', reveal_50: 'RÉVÉLATION INTERMÉDIAIRE' };
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const messageOf = (value) => typeof value === 'string' ? value : value?.message || value?.detail || JSON.stringify(value);
const studioPages = (book = state.book) => [...(book?.pages || []), ...state.pendingPhotos.filter(page => page.bookId === book?.id)];
const currentPage = () => studioPages().find(page => page.id === state.pageId) || null;
const selectedVariant = page => page?.variants?.find(variant => variant.id === page.selected_variant_id) || null;
const activeVariant = () => {
  const page = currentPage();
  const desired = state.photoUniverses.get(page?.id);
  return page?.variants?.find(variant => variant.id === state.variantId) || (desired ? findMatchingVariant(page, desired) : null) || selectedVariant(page) || page?.variants?.at(-1) || null;
};
function universeParts(key = 'original') {
  const [head, palette] = String(key || 'original').split('__');
  return head.startsWith('art_') ? { art: head, palette: palette || 'original' } : { art: 'original', palette: head };
}
function universeKey(art, palette = 'original') { return art === 'original' ? palette : palette === 'original' ? art : `${art}__${palette}`; }
function universeItem(key) { return state.catalog.universes.find(item => item.key === key); }
function universeLabel(key) {
  const { art, palette } = universeParts(key);
  if (art !== 'original') return `${universeItem(art)?.label || art}${palette === 'original' ? '' : ` · ${universeItem(palette)?.label || palette}`}`;
  return universeItem(palette)?.label || key || 'Fidèle à la photo';
}
function validUniverse(key) {
  const { art, palette } = universeParts(key);
  return !!universeItem(palette) && universeItem(palette).kind !== 'artistic' && (art === 'original' || universeItem(art)?.kind === 'artistic');
}
function isIllustrated(variant) { return variant?.config?.artwork_mode === 'premium' || universeParts(variant?.universe).art !== 'original'; }
function useNewDrawingDefaults() {
  $('#artwork-mode').value = state.catalog.premium_enabled ? 'premium' : 'local';
  $('#colors-select').value = '32'; $('#difficulty-select').value = 'expert';
  $('#regions-select').value = '3200'; $('#masking-select').value = 'mystery';
}
function artisticAvailable(art) { return art === 'original' || (universeItem(art)?.available ?? state.catalog.artistic_enabled ?? state.catalog.premium_enabled ?? false); }
const validatedCount = () => state.book?.validated_count ?? state.book?.pages?.filter(page => page.selected_variant_id).length ?? 0;
const warningsFor = (page, variant) => [...new Set([...(page?.warnings || []), ...(variant?.warnings || []), ...(variant?.quality?.warnings || [])].map(messageOf))];
const qualityProblems = variant => {
  const quality = variant?.quality || {};
  const problems = [...(quality.errors || []), ...(quality.critical_errors || [])].map(messageOf);
  if (quality.label_coverage !== undefined && Number(quality.label_coverage) < 1 && !problems.length) problems.push('Certaines zones ne possèdent pas encore de code lisible.');
  return problems;
};
const safeUrl = value => {
  if (!value || typeof value !== 'string') return '';
  try {
    const url = new URL(value, window.location.origin);
    if (localObjectUrls.has(value)) return value;
    if (url.origin === window.location.origin && ['http:', 'https:'].includes(url.protocol)) return url.pathname + url.search;
    if (url.protocol === 'https:' && !url.username && !url.password && !url.port && url.hostname.endsWith('.blob.vercel-storage.com')) return url.href;
    return '';
  } catch { return ''; }
};

async function requestApi(path, { method = 'GET', body, signal } = {}) {
  const startedEpoch = mutationEpoch;
  const options = { method, credentials: 'same-origin', signal, headers: { Accept: 'application/json' } };
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) { options.body = JSON.stringify(body); options.headers['Content-Type'] = 'application/json'; }
  let response;
  try { response = await fetch(path, options); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('L’atelier ne répond pas. Vérifiez que le serveur est lancé, puis réessayez.');
  }
  let data = null;
  if (response.status !== 204) {
    try { data = await response.json(); }
    catch { if (response.ok) throw new Error('Une réponse de l’atelier est incomplète. Réessayez.'); }
  }
  if (!response.ok) {
    const detail = data?.detail || data?.message || `Le traitement a échoué (${response.status}).`;
    const error = new Error(Array.isArray(detail) ? detail.map(item => item.msg || messageOf(item)).join(' · ') : messageOf(detail));
    error.status = response.status;
    throw error;
  }
  const epoch = method === 'GET' ? startedEpoch : ++mutationEpoch;
  if (data && typeof data === 'object') { responseEpochs.set(data, epoch); if (data.book) responseEpochs.set(data.book, epoch); }
  return data;
}

// Only one state-changing request at a time: Vercel saves one atomic book snapshot.
function api(path, options = {}) {
  const method = options.method || 'GET';
  if (['GET', 'HEAD', 'OPTIONS'].includes(method) || /\/uploads\/prepare(?:-batch)?$/.test(path)) return requestApi(path, options);
  const request = writeTail.then(() => requestApi(path, options));
  writeTail = request.catch(() => undefined);
  return request;
}

function toast(message, error = false, duration = 6500) {
  const node = document.createElement('div');
  node.className = `toast${error ? ' error' : ''}`;
  const text = document.createElement('span'); text.textContent = message;
  const close = document.createElement('button'); close.type = 'button'; close.textContent = '×'; close.setAttribute('aria-label', 'Fermer la notification');
  close.addEventListener('click', () => node.remove());
  node.append(text, close); $('#toasts').append(node);
  if (duration) window.setTimeout(() => node.remove(), duration);
}

async function confirmChoice(title, description, button = 'Confirmer', eyebrow = 'VOTRE CRÉATION') {
  const dialog = $('#confirm-dialog');
  $('#confirm-title').textContent = title;
  $('#confirm-eyebrow').textContent = eyebrow;
  $('#confirm-accept').textContent = button;
  $('#confirm-description').replaceChildren();
  const lines = Array.isArray(description) ? description : [description];
  for (const line of lines) { const p = document.createElement('p'); p.textContent = line; $('#confirm-description').append(p); }
  dialog.returnValue = '';
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    dialog.showModal();
  });
}

function setSaveStatus(text, status = '') {
  $('#save-status').textContent = text;
  $('#save-dot').className = `save-indicator${status ? ` ${status}` : ''}`;
}

function formPayload() {
  return {
    title: $('#book-title').value.trim(), recipient: $('#book-recipient').value.trim(),
    dedication: $('#book-dedication').value.trim(),
    template: $('input[name="template"]:checked').value,
    target_photos: Number($('#target-photos').value),
  };
}

function fillBookForm(book) {
  $('#book-title').value = book?.title || 'Nos plus beaux souvenirs';
  $('#book-recipient').value = book?.recipient || '';
  $('#book-dedication').value = book?.dedication || '';
  $('#target-photos').value = book?.target_photos || 8;
  const radio = $(`input[name="template"][value="${['heritage', 'nocturne', 'botanique'].includes(book?.template) ? book.template : 'heritage'}"]`);
  if (radio) radio.checked = true;
  $('#create-book').innerHTML = book ? 'Retrouver mes souvenirs <span aria-hidden="true">→</span>' : 'Commencer mon livre <span aria-hidden="true">→</span>';
}

function setBook(book, { fillForm = false } = {}) {
  const previousRevision = state.book?.revision;
  const previousId = state.book?.id;
  if (previousId && previousId !== book.id && !fillForm) return;
  const epoch = responseEpochs.get(book);
  if (previousId === book.id && epoch !== undefined && epoch < state.bookEpoch) return;
  state.bookEpoch = epoch ?? mutationEpoch;
  state.book = book; state.assetUrlsAt = Date.now();
  if (!studioPages(book).some(page => page.id === state.pageId)) {
    state.pageId = book.pages?.[0]?.id || null;
    state.variantId = null;
  }
  if (previousRevision !== book.revision || previousId !== book.id) {
    state.export = null;
    $('#approval-checkbox').checked = false;
  }
  if (fillForm) { fillBookForm(book); state.metadataDirty = false; }
  try { localStorage.setItem('colorart.activeBook', book.id); } catch { /* Reprise facultative si stockage désactivé. */ }
  const index = state.books.findIndex(item => item.id === book.id);
  if (index < 0) state.books.unshift(book); else state.books[index] = book;
  renderProjects(); renderProgress(); renderPhotos(); renderComposition(); renderGift();
}

function renderProjects() {
  const select = $('#project-select');
  select.innerHTML = '<option value="">Nouveau livre</option>' + state.books.map(book => `<option value="${escapeHTML(book.id)}">${escapeHTML(book.title || 'Mon livre')}</option>`).join('');
  select.value = state.book?.id || '';
}

function renderProgress() {
  const count = validatedCount(), target = state.book?.target_photos || Number($('#target-photos').value) || 8;
  $('#sidebar-count').textContent = `${count} / ${target}`;
  $('#book-progress').max = target; $('#book-progress').value = Math.min(count, target);
  $('#progress-caption').textContent = !state.book ? 'Choisissez votre collection pour commencer.' : count === target ? 'Chaque souvenir a trouvé sa place.' : `${target - count} illustration${target - count > 1 ? 's' : ''} à choisir pour compléter votre livre.`;
  const completed = [!!state.book, count === target && target > 0, count === target && target > 0, !!state.export];
  $$('.step').forEach((button, index) => { $('.step-check', button).textContent = completed[index] ? '✓' : ''; });
}

async function saveMetadata() {
  window.clearTimeout(saveTimer);
  if (state.metadataSaving) { await state.metadataSaving; if (state.metadataDirty) return saveMetadata(); return; }
  if (!state.book || !state.metadataDirty) return;
  if (!$('#book-form').checkValidity()) { setSaveStatus('Complétez les informations du livre', 'error'); throw new Error('Vérifiez le titre et le nombre de photos de votre livre.'); }
  const bookId = state.book.id, version = state.metadataVersion, payload = formPayload();
  state.metadataDirty = false;
  setSaveStatus('Enregistrement de vos modifications…', 'saving');
  state.metadataSaving = (async () => {
    try {
      const book = await api(`/api/books/${bookId}`, { method: 'PATCH', body: payload });
      if (state.book?.id === bookId) setBook(book);
      if (version === state.metadataVersion) setSaveStatus('Toutes les modifications sont enregistrées');
    } catch (error) {
      if (state.book?.id === bookId) state.metadataDirty = true;
      setSaveStatus('Modifications non enregistrées', 'error');
      throw error;
    } finally { state.metadataSaving = null; }
  })();
  await state.metadataSaving;
  if (state.metadataDirty && version !== state.metadataVersion) return saveMetadata();
}

function scheduleSave() {
  state.metadataVersion += 1;
  if (!state.book) { renderProgress(); return; }
  state.metadataDirty = true;
  $('#approval-checkbox').checked = false; updateExportButton();
  setSaveStatus('Modifications à enregistrer…', 'saving');
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => saveMetadata().catch(error => toast(error.message, true)), 850);
}

async function goTo(step, { focus = true } = {}) {
  if (step > 1 && !state.book) { toast('Choisissez votre collection et commencez votre livre pour ajouter des photos.'); step = 1; }
  if (state.book && state.metadataDirty) await saveMetadata();
  state.step = step;
  $$('.stage').forEach((section, index) => { section.hidden = index + 1 !== step; });
  $$('.step').forEach((button, index) => {
    button.classList.toggle('active', index + 1 === step);
    if (index + 1 === step) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
  });
  $('#stage-eyebrow').textContent = `ÉTAPE ${String(step).padStart(2, '0')} — ${stageNames[step - 1]}`;
  if (step === 3) renderComposition();
  if (step === 4) { renderGift(); refreshReadiness().catch(error => toast(error.message, true)); }
  if (focus) { window.scrollTo({ top: 0, behavior: 'smooth' }); $(`#title-${step}`).setAttribute('tabindex', '-1'); $(`#title-${step}`).focus({ preventScroll: true }); }
}

async function openBook(id) {
  if (hasPendingFavoriteGeneration()) { renderProjects(); toast('Votre coup de cœur s’enregistre. Vous pouvez parcourir les photos de ce livre pendant ce temps.'); return; }
  if (state.uploading) { renderProjects(); toast('Vos photos sont en cours d’enregistrement. Vous pouvez les parcourir pendant ce temps.'); return; }
  if (state.metadataDirty) await saveMetadata();
  const opening = ++bookOpenSequence;
  const book = await api(`/api/books/${id}`);
  if (opening !== bookOpenSequence) return;
  releaseBookObjectUrls();
  state.pageId = null; state.variantId = null;
  setBook(book, { fillForm: true });
  const restoredVariant = activeVariant();
  if (restoredVariant) adoptVariantSettings(restoredVariant); else useNewDrawingDefaults();
  renderPhotos();
  setSaveStatus('Votre livre a été retrouvé');
  await goTo(book.pages?.length ? 2 : 1);
}

function photoUrl(page) { return safeUrl(page?.localUrl || state.localPhotos.get(page?.id) || page?.source_url || page?.thumbnail_url); }
function thumbnailUrl(page) { return safeUrl(page?.localUrl || state.localPhotos.get(page?.id) || page?.thumbnail_url || page?.source_url); }
function variantUrl(variant, kind = state.view) { return safeUrl(variant?.files?.[kind]); }

function adoptVariantSettings(variant) {
  const config = variant.config || {};
  for (const [key, id] of Object.entries({ n_colors: 'colors-select', difficulty: 'difficulty-select', fit_mode: 'fit-select', artwork_mode: 'artwork-mode', target_regions: 'regions-select', masking: 'masking-select' })) {
    if (key === 'artwork_mode' && universeParts(variant.universe).art !== 'original') continue;
    if (config[key] !== undefined && [...$(`#${id}`).options].some(option => option.value === String(config[key]))) $(`#${id}`).value = String(config[key]);
  }
}
function setUniverseSelection(universe) {
  const select = $('#universe-select');
  if (![...select.options].some(option => option.value === universe)) {
    const option = document.createElement('option'); option.value = universe; option.textContent = universeLabel(universe); select.append(option);
  }
  select.value = universe;
}
function releaseBookObjectUrls() {
  for (const url of state.localPhotos.values()) { URL.revokeObjectURL(url); localObjectUrls.delete(url); }
  state.localPhotos.clear(); previewBundles.clear();
}

function renderConfig(universe = null) {
  const page = currentPage();
  const selected = universe || state.photoUniverses.get(page?.id) || page?.variants?.find(item => item.id === state.variantId)?.universe || selectedVariant(page)?.universe || page?.variants?.at(-1)?.universe || 'original';
  return { n_colors: Number($('#colors-select').value || 32), difficulty: $('#difficulty-select').value || 'expert',
    fit_mode: $('#fit-select').value || 'contain', artwork_mode: universeParts(selected).art !== 'original' ? 'premium' : $('#artwork-mode').value || 'local',
    target_regions: Number($('#regions-select').value || 3200), masking: $('#masking-select').value || 'mystery' };
}
function configKey(config = renderConfig()) { return JSON.stringify(['n_colors','difficulty','fit_mode','artwork_mode','target_regions','masking'].map(key => config[key])); }
function configMatches(variant, config = renderConfig()) {
  const value = variant?.config || {};
  return Number(value.n_colors) === config.n_colors && value.difficulty === config.difficulty && value.fit_mode === config.fit_mode
    && (value.artwork_mode || 'local') === config.artwork_mode && Number(value.target_regions || 3200) === config.target_regions
    && (value.masking || 'mystery') === config.masking
    && (config.artwork_mode !== 'premium' || value.preserve_details === true);
}
function findMatchingVariant(page, universe, config = renderConfig(universe)) { return [...(page?.variants || [])].reverse().find(variant => variant.universe === universe && configMatches(variant, config)); }
function desiredUniverse(page) { return state.photoUniverses.get(page?.id) || activeVariant()?.universe || 'original'; }
function choiceKey(pageId, universe, config = renderConfig()) { return `${state.book?.id}:${pageId}:${universe}:${configKey(config)}`; }
function favoriteValue(variant) { return state.favoriteIntents.has(variant.id) ? state.favoriteIntents.get(variant.id) : !!variant.favorite; }
function reusableIllustration(page, universe) {
  const scene = universeParts(universe).art;
  return [...(page?.variants || [])].reverse().find(item => isIllustrated(item)
    && (item.config?.art_universe || universeParts(item.universe).art) === scene
    && item.files?.source && !qualityProblems(item).length);
}
function sameSceneVariant(page, universe, config = renderConfig(universe)) {
  const scene = universeParts(universe).art;
  return [...(page?.variants || [])].reverse().find(item => (item.config?.art_universe || universeParts(item.universe).art) === scene && configMatches(item, config));
}

const universeColors = ['#bfa173','#cba4ad','#a4bfc5','#b2bba3','#a6a1bd','#cdaf80','#9eada3','#b9a1a4','#b0b6ca','#c2ab8d','#9eb5b1','#c6b5c9','#b8b294','#a9b5c1','#c3a28c','#b2b4b1'];
function renderUniverseRail(page, desired) {
  const rail = $('#universe-rail'); if (!rail) return;
  const selected = universeParts(desired || 'original'), colors = state.universeTab === 'palette';
  const universes = (state.catalog.universes || []).filter(item => colors ? item.kind !== 'artistic' : item.key === 'original' || item.kind === 'artistic');
  $$('[data-universe-tab]').forEach(button => { const active = button.dataset.universeTab === state.universeTab; button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1; });
  rail.setAttribute('aria-labelledby', colors ? 'universe-tab-colors' : 'universe-tab-artistic');
  $('#universe-category-note').textContent = colors ? 'La même scène, une nouvelle harmonie de couleurs.' : '15 nouveaux univers, et les 3 univers créatifs d’origine.';
  rail.innerHTML = universes.map((item, index) => {
    const key = colors ? universeKey(selected.art, item.key) : item.key;
    const ready = findMatchingVariant(page, key), active = colors ? selected.palette === item.key : selected.art === item.key;
    const processing = !!state.job && !!page && state.job.pageId === page.id && state.job.universe === key;
    const queued = !!state.queuedGeneration && !!page && state.queuedGeneration.pageId === page.id && state.queuedGeneration.universe === key;
    const failed = page && state.failedChoices.has(choiceKey(page.id, key, renderConfig(key)));
    const unavailable = item.kind === 'artistic' && !artisticAvailable(item.key) && !sameSceneVariant(page, key) && !reusableIllustration(page, key);
    const note = processing ? 'Création en cours…' : queued ? 'Votre prochain choix' : failed ? 'Toucher pour réessayer' : ready ? (favoriteValue(ready) ? '♥ Coup de cœur' : 'Prêt à afficher') : unavailable ? 'Service à activer' : colors ? 'Toucher pour appliquer' : item.key === 'original' ? 'La photo comme référence' : 'Créer cet univers';
    const accent = /^#[0-9a-f]{3,8}$/i.test(item.accent || '') ? item.accent : universeColors[index % universeColors.length];
    const label = colors && item.key === 'original' ? 'Couleurs d’origine' : item.label;
    return `<button class="universe-choice${item.kind === 'artistic' ? ' universe-choice-art' : ''}${unavailable ? ' is-unavailable' : ''}" data-universe="${escapeHTML(key)}" type="button" aria-pressed="${active}" ${unavailable ? 'aria-disabled="true"' : ''} ${!page ? 'disabled' : ''}><span class="universe-swatch" style="--universe-color:${accent}" aria-hidden="true">${colors ? '' : item.key === 'original' ? '✧' : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M4 17.5 9 11l4 4 3-3 4 5.5M4 19h16M17 4v5m-2.5-2.5h5"/><circle cx="7" cy="6" r="2"/></svg>'}</span><span class="universe-choice-copy"><span class="universe-choice-name">${escapeHTML(label)}${item.is_new && !colors ? '<span class="universe-new">Nouveau</span>' : ''}</span><span class="universe-choice-note">${escapeHTML(note)}</span></span></button>`;
  }).join('');
}

function selectPhoto(id) {
  if (!studioPages().some(page => page.id === id)) return;
  state.pageId = id; state.variantId = null;
  const variant = activeVariant(); if (variant) adoptVariantSettings(variant); else useNewDrawingDefaults();
  renderPhotos();
  if (Date.now() - state.assetUrlsAt > 4 * 60 * 1000) refreshAssetUrls();
  const thumbnail = $(`[data-photo="${id}"]`); thumbnail?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
}
function movePhoto(direction) { const pages = studioPages(), next = pages.findIndex(page => page.id === state.pageId) + direction; if (next >= 0 && next < pages.length) selectPhoto(pages[next].id); }

function requestUniverse(universe, { force = false } = {}) {
  const page = currentPage(); if (!page || !validUniverse(universe)) return;
  const config = renderConfig(universe), seq = ++choiceSequence;
  const ready = !force && findMatchingVariant(page, universe, config), { art } = universeParts(universe);
  if (!ready && !artisticAvailable(art) && !reusableIllustration(page, universe) && (force || !sameSceneVariant(page, universe, config))) {
    state.universeNotice = 'La création des univers artistiques nécessite l’activation du service artistique de cet atelier. Vos photos et vos dessins restent disponibles.';
    renderUniverseDescription(); toast(state.universeNotice, true, 7000); return;
  }
  state.universeNotice = '';
  state.photoUniverses.set(page.id, universe); state.choiceSequences.set(page.id, seq);
  state.failedChoices.delete(choiceKey(page.id, universe, config));
  setUniverseSelection(universe); renderUniverseDescription();
  if (ready) {
    state.variantId = ready.id;
    if (state.queuedGeneration?.pageId === page.id) state.queuedGeneration = null;
    renderPhotos(); return;
  }
  state.variantId = null;
  if (!force && state.job?.bookId === state.book.id && state.job.pageId === page.id && state.job.universe === universe && configKey(state.job.config) === configKey(config)) {
    state.job.choiceSeq = seq; state.queuedGeneration = null; renderPhotos(); return;
  }
  if (!page.pending) state.queuedGeneration = { bookId: state.book.id, pageId: page.id, universe, config, choiceSeq: seq };
  renderPhotos();
  window.clearTimeout(generationTimer);
  generationTimer = window.setTimeout(() => drainGenerationQueue(), 220);
}

function generationChoiceKey(request) { return `${request.bookId}:${request.pageId}:${request.universe}:${configKey(request.config)}`; }
function hasPendingFavoriteGeneration() {
  return !!state.book && [...state.favoriteIntents.entries()].some(([key, desired]) => desired && key.startsWith(`${state.book.id}:`));
}
async function drainGenerationQueue() {
  if (state.job || state.uploading) return;
  const request = state.queuedGeneration || state.pinnedGenerations.values().next().value;
  if (!request) return;
  state.queuedGeneration = null;
  state.pinnedGenerations.delete(generationChoiceKey(request));
  if (state.book?.id !== request.bookId || !state.book.pages.some(page => page.id === request.pageId)) {
    state.favoriteIntents.delete(generationChoiceKey(request));
    if (state.queuedGeneration || state.pinnedGenerations.size) window.setTimeout(() => drainGenerationQueue(), 0);
    return;
  }
  state.job = { ...request, id: null, status: 'starting', progress: null, startedAt: Date.now(),
    stage: request.config.artwork_mode === 'premium' ? 'Création du dessin illustré et préparation de votre page.' : 'Préparation de l’étude de découpage.' };
  elapsedTimer = window.setInterval(renderJob, 1000);
  renderPhotos();
  try {
    await saveMetadata();
    const job = await api(`/api/books/${request.bookId}/photos/${request.pageId}/generate`, { method: 'POST', body: { ...request.config, universe: request.universe } });
    state.job = { ...state.job, ...request, ...job }; renderJob();
    await pollJob();
  } catch (error) {
    state.job = null; state.failedChoices.add(choiceKey(request.pageId, request.universe, request.config));
    const failedKey = generationChoiceKey(request);
    if (state.favoriteIntents.get(failedKey)) toast('Ce coup de cœur n’a pas pu être enregistré. Touchez à nouveau son univers pour réessayer.', true);
    state.favoriteIntents.delete(failedKey); renderJob();
    toast(`${error.message} Votre photo est conservée.`, true, 10000);
  } finally {
    window.clearInterval(elapsedTimer); elapsedTimer = null;
    if (state.book?.id === request.bookId) renderPhotos();
    if (state.queuedGeneration || state.pinnedGenerations.size) window.setTimeout(() => drainGenerationQueue(), 0);
  }
}

async function toggleFavorite(page, variant, desired = !favoriteValue(variant)) {
  if (!page || !variant || state.favoriteBusy.has(variant.id)) return;
  const bookId = state.book.id;
  if (!!variant.favorite === desired && !state.favoriteIntents.has(variant.id)) return;
  state.favoriteBusy.add(variant.id); state.favoriteIntents.set(variant.id, desired); renderPhotos();
  try {
    const book = await api(`/api/books/${bookId}/photos/${page.id}/variants/${variant.id}/favorite`, { method: 'POST' });
    if (state.book?.id === bookId) setBook(book);
  } catch (error) {
    toast(`Le favori n’a pas pu être enregistré. ${error.message}`, true);
  } finally { state.favoriteIntents.delete(variant.id); state.favoriteBusy.delete(variant.id); if (state.book?.id === bookId) renderPhotos(); }
}
async function toggleCurrentFavorite() {
  const page = currentPage(); if (!page || page.pending) return;
  const universe = desiredUniverse(page), variant = findMatchingVariant(page, universe);
  if (variant) return toggleFavorite(page, variant);
  const config = renderConfig(), key = choiceKey(page.id, universe, config), value = !state.favoriteIntents.get(key);
  const alreadyRunning = state.job?.bookId === state.book.id && state.job.pageId === page.id && state.job.universe === universe && configKey(state.job.config) === configKey(config);
  const limit = Math.min(80, Math.max(1, state.catalog.universes.length) * Math.max(1, state.book.pages.length));
  if (value && !alreadyRunning && !state.pinnedGenerations.has(key) && state.pinnedGenerations.size >= limit) {
    toast('Vos coups de cœur s’enregistrent. Attendez un instant avant d’en ajouter un autre.'); return;
  }
  if (value) state.favoriteIntents.set(key, true); else state.favoriteIntents.delete(key);
  if (value && !alreadyRunning) state.pinnedGenerations.set(key, {
    bookId: state.book.id, pageId: page.id, universe, config,
    choiceSeq: state.choiceSequences.get(page.id),
  });
  else state.pinnedGenerations.delete(key);
  renderPhotos();
  window.setTimeout(() => drainGenerationQueue(), 0);
}
async function flushFavoriteIntent(page, variant, config) {
  const key = choiceKey(page.id, variant.universe, config);
  if (!state.favoriteIntents.has(key)) return;
  const desired = state.favoriteIntents.get(key); state.favoriteIntents.delete(key);
  if (!!variant.favorite !== desired) await toggleFavorite(page, variant, desired);
}

async function primePreview(page, variant, universe = variant?.universe || desiredUniverse(page)) {
  const scene = universeParts(universe).art, config = renderConfig(universe);
  const eligible = item => (item.config?.art_universe || universeParts(item.universe).art) === scene && configMatches(item, config) && item.files?.studio_preview && item.files?.studio_index;
  const candidate = [...(page?.variants || [])].reverse().find(item => eligible(item) && universeParts(item.universe).palette === 'original')
    || (variant && eligible(variant) ? variant : null)
    || [...(page?.variants || [])].reverse().find(eligible);
  if (!candidate) return null;
  const key = `${state.book?.id}:${page.id}:${candidate.id}`;
  if (!previewBundles.has(key)) {
    const pending = (async () => {
      const descriptorUrl = safeUrl(candidate.files.studio_preview), indexUrl = safeUrl(candidate.files.studio_index);
      if (!descriptorUrl || !indexUrl) throw new Error('Aperçu indisponible');
      const [descriptionResponse, imageResponse] = await Promise.all([
        fetch(descriptorUrl, { credentials: 'same-origin' }), fetch(indexUrl, { credentials: 'same-origin' }),
      ]);
      if (!descriptionResponse.ok || !imageResponse.ok) throw new Error('Aperçu indisponible');
      const description = await descriptionResponse.json();
      if (description.schema_version !== 1 || !description.palettes || !Array.isArray(description.ink_color)) throw new Error('Aperçu invalide');
      const blob = await imageResponse.blob();
      let bitmap;
      if (window.createImageBitmap) bitmap = await createImageBitmap(blob);
      else {
        bitmap = await new Promise((resolve, reject) => {
          const url = URL.createObjectURL(blob), image = new Image();
          image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
          image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Aperçu indisponible')); }; image.src = url;
        });
      }
      if (bitmap.width > 1000 || bitmap.height > 1500) { bitmap.close?.(); throw new Error('Aperçu trop volumineux'); }
      const canvas = document.createElement('canvas'); canvas.width = bitmap.width; canvas.height = bitmap.height;
      const context = canvas.getContext('2d', { willReadFrequently: true }); context.drawImage(bitmap, 0, 0); bitmap.close?.();
      return { description, width: canvas.width, height: canvas.height, pixels: context.getImageData(0, 0, canvas.width, canvas.height).data };
    })();
    previewBundles.set(key, pending);
    while (previewBundles.size > 10) previewBundles.delete(previewBundles.keys().next().value);
    pending.catch(() => { if (previewBundles.get(key) === pending) previewBundles.delete(key); });
  }
  return previewBundles.get(key);
}

async function renderInstantPreview(page, universe) {
  const bookId = state.book.id, seq = state.choiceSequences.get(page.id);
  if (state.view !== 'solution_final') return;
  let bundle;
  try { bundle = await primePreview(page, activeVariant(), universe); } catch { return; }
  if (!bundle || state.book?.id !== bookId || state.pageId !== page.id || desiredUniverse(currentPage()) !== universe || state.choiceSequences.get(page.id) !== seq || state.view !== 'solution_final' || findMatchingVariant(currentPage(), universe)) return;
  const colors = bundle.description.palettes[universeParts(universe).palette]; if (!Array.isArray(colors)) return;
  let canvas = $('#studio-preview-canvas');
  if (!canvas) { canvas = document.createElement('canvas'); canvas.id = 'studio-preview-canvas'; canvas.setAttribute('role', 'img'); $('#result-stage').prepend(canvas); }
  canvas.width = bundle.width; canvas.height = bundle.height;
  const context = canvas.getContext('2d'), output = context.createImageData(bundle.width, bundle.height);
  const data = output.data, encoded = bundle.pixels, ink = bundle.description.ink_color, composition = bundle.description.ink_composition || 'multiply';
  for (let offset = 0; offset < data.length; offset += 4) {
    const color = colors[encoded[offset]]; if (!Array.isArray(color)) return;
    for (let channel = 0; channel < 3; channel += 1) {
      const base = color[channel], inkValue = ink[channel];
      data[offset + channel] = encoded[offset + 1] ? composition === 'multiply' ? Math.floor((base * inkValue + 127) / 255) : composition === 'darken' ? Math.min(base, inkValue) : inkValue : base;
    }
    data[offset + 3] = 255;
  }
  context.putImageData(output, 0, 0); canvas.hidden = false;
  canvas.dataset.bookId = bookId; canvas.dataset.pageId = page.id; canvas.dataset.universe = universe;
  canvas.setAttribute('aria-label', `Aperçu ${universeLabel(universe)} en cours d’enregistrement`);
  $('#result-image').hidden = true; $('#result-placeholder').hidden = true;
  const failed = state.failedChoices.has(choiceKey(page.id, universe));
  $('#result-label').hidden = false; $('#result-label').textContent = failed ? 'APERÇU · NON ENREGISTRÉ' : 'APERÇU · ENREGISTREMENT';
  $('#result-caption').textContent = failed ? `${universeLabel(universe)} · Aperçu non enregistré. Touchez l’univers pour réessayer.` : `${universeLabel(universe)} · Aperçu immédiat, dessin en cours d’enregistrement`;
}

let assetRefreshPromise = null, lastAssetRefresh = 0;
function refreshAssetUrls() {
  if (!state.book || assetRefreshPromise || Date.now() - lastAssetRefresh < 60000) return assetRefreshPromise;
  const bookId = state.book.id; lastAssetRefresh = Date.now();
  assetRefreshPromise = api(`/api/books/${bookId}`).then(book => {
    if (state.book?.id === bookId) setBook(book);
  }).catch(() => undefined).finally(() => { assetRefreshPromise = null; });
  return assetRefreshPromise;
}

const localPhotoLoads = new Set();
function releaseLocalPhotoWhenReady(page) {
  const local = state.localPhotos.get(page.id), source = safeUrl(page.source_url), thumbnail = safeUrl(page.thumbnail_url);
  if (!local || !source || localPhotoLoads.has(page.id)) return;
  localPhotoLoads.add(page.id);
  const load = url => new Promise((resolve, reject) => { const image = new Image(); image.onload = resolve; image.onerror = reject; image.src = url; });
  Promise.all([load(source), thumbnail ? load(thumbnail) : Promise.resolve()]).then(() => {
    if (state.localPhotos.get(page.id) !== local) return;
    state.localPhotos.delete(page.id);
    if (state.pageId === page.id) renderPhotos();
    requestAnimationFrame(() => { URL.revokeObjectURL(local); localObjectUrls.delete(local); });
  }).catch(() => undefined).finally(() => localPhotoLoads.delete(page.id));
}

function renderPhotos() {
  const pages = studioPages();
  $('#photo-count-badge').textContent = `${pages.length} / ${state.book?.target_photos || 8} photos`;
  $('#photo-strip').innerHTML = pages.map((page, index) => `<button class="photo-thumb${page.id === state.pageId ? ' active' : ''}" type="button" data-photo="${escapeHTML(page.id)}" aria-label="Photo ${index + 1} : ${escapeHTML(page.filename)}${page.pending ? ', enregistrement en cours' : ''}" aria-pressed="${page.id === state.pageId}"><img src="${escapeHTML(thumbnailUrl(page))}" alt="" loading="lazy"><span>${String(index + 1).padStart(2, '0')}</span>${page.pending ? '<span class="validated-dot" aria-hidden="true">…</span>' : page.selected_variant_id ? '<span class="validated-dot" aria-hidden="true">✓</span>' : ''}</button>`).join('');
  $('#no-photos').hidden = pages.length > 0;
  $('#photo-workspace').hidden = pages.length === 0;
  renderJob();
  if (!pages.length) return;
  if (!currentPage()) state.pageId = pages[0].id;
  const page = currentPage(), index = pages.indexOf(page);
  const desired = desiredUniverse(page), saved = findMatchingVariant(page, desired);
  const pending = !saved && !!state.photoUniverses.get(page.id), variant = pending ? null : saved || activeVariant();
  if (!page.pending) releaseLocalPhotoWhenReady(page);
  $('#current-photo-index').textContent = `PHOTO ${String(index + 1).padStart(2, '0')} / ${pages.length}`;
  $('#current-photo-position').textContent = `Photo ${index + 1} sur ${pages.length}`;
  $('#prev-photo').disabled = index <= 0; $('#next-photo').disabled = index >= pages.length - 1;
  $('#current-photo-name').textContent = page.filename || 'Votre souvenir';
  const source = photoUrl(page); if ($('#source-image').getAttribute('src') !== source) $('#source-image').src = source;
  $('#source-image').alt = `Photo originale : ${page.filename || 'votre souvenir'}`;
  const result = variantUrl(variant);
  $('#result-image').hidden = !result;
  if (result && $('#result-image').getAttribute('src') !== result) $('#result-image').src = result;
  else if (!result) $('#result-image').removeAttribute('src');
  $('#result-image').alt = `${state.view === 'mystery' ? 'Page à colorier' : 'Dessin colorié'} : ${page.filename || 'votre souvenir'}`;
  const canvas = $('#studio-preview-canvas');
  const keepPreview = pending && state.view === 'solution_final' && canvas?.dataset.bookId === state.book.id && canvas.dataset.pageId === page.id && canvas.dataset.universe === desired;
  if (canvas) canvas.hidden = !keepPreview;
  $('#result-placeholder').hidden = !!result || !!keepPreview;
  const creatingScene = pending && universeParts(desired).art !== 'original' && !sameSceneVariant(page, desired);
  const failed = state.failedChoices.has(choiceKey(page.id, desired));
  $('#result-placeholder h4').textContent = failed ? 'Votre dessin n’est pas encore prêt' : creatingScene ? 'Création de votre univers…' : pending ? 'Votre dessin prend forme…' : 'Et si votre souvenir devenait un dessin ?';
  $('#result-placeholder p').textContent = failed ? 'Votre photo est conservée. Touchez à nouveau votre choix pour réessayer.' : creatingScene ? 'Un nouveau décor est illustré à partir de votre photo. Vous pouvez parcourir vos autres souvenirs.' : pending ? 'Votre choix s’enregistre. Vous pouvez continuer à parcourir vos photos.' : 'Choisissez un univers artistique ou gardez votre photo comme référence.';
  $('#result-stage').setAttribute('aria-busy', String(pending && !failed));
  if (keepPreview) $('#result-image').hidden = true;
  $('#result-label').hidden = !result; $('#result-label').textContent = variant && !isIllustrated(variant) ? (state.view === 'solution_final' ? 'ÉTUDE DE DÉCOUPAGE' : viewNames[state.view]) : viewNames[state.view];
  $('#enlarge-result').hidden = !variant || pending;
  $('#result-caption').textContent = page.pending ? 'Votre photo est visible. Elle s’enregistre en arrière-plan.' : pending ? (state.failedChoices.has(choiceKey(page.id, desired)) ? `${universeLabel(desired)} · Aperçu non enregistré. Touchez l’univers pour réessayer.` : `${universeLabel(desired)} · Préparation de votre dessin…`) : variant ? `${universeLabel(variant.universe)} · ${isIllustrated(variant) ? 'Dessin illustré enregistré' : 'Étude de découpage enregistrée'}` : 'Touchez un univers à droite pour créer votre dessin.';
  $('#drawing-kind-note').hidden = !variant || isIllustrated(variant);
  $('#drawing-kind-note').textContent = 'Cette version découpe directement la photo. Le dessin illustré crée d’abord un véritable trait dessiné.';
  $('#create-premium-button').hidden = !variant || (isIllustrated(variant) && variant.config?.preserve_details === true) || (!state.catalog.premium_enabled && !reusableIllustration(page, desired));
  $('#create-premium-button').textContent = isIllustrated(variant) ? 'Restaurer les détails du dessin' : 'Créer le dessin illustré';
  const guide = safeUrl(variant?.files?.detail_guides);
  $('#detail-guide-link').hidden = !guide;
  if (guide) $('#detail-guide-link').href = guide; else $('#detail-guide-link').removeAttribute('href');
  $('#create-premium-button').disabled = !!state.job || !!page.pending || state.uploading;
  $('#selected-label').textContent = page.selected_variant_id ? '✓ Une version retenue pour le livre' : 'Choisissez votre dessin préféré pour cette page';
  $('#remove-photo').disabled = !!page.pending || state.job?.pageId === page.id;
  $('#generate-button').disabled = !!page.pending || state.uploading || !!state.job;
  const variants = [...(page.variants || [])].reverse().filter(item => !state.favoritesOnly || favoriteValue(item));
  $('#variants-grid').innerHTML = variants.length ? variants.map(item => `<article class="variant-card${variant?.id === item.id && !pending ? ' active' : ''}${page.selected_variant_id === item.id ? ' selected' : ''}"><button class="variant-image-button" type="button" data-variant="${escapeHTML(item.id)}" aria-label="Afficher ${escapeHTML(universeLabel(item.universe))}"><img src="${escapeHTML(variantUrl(item, 'solution_final'))}" alt="${escapeHTML(universeLabel(item.universe))}" loading="lazy"></button><button class="variant-favorite" type="button" data-favorite="${escapeHTML(item.id)}" aria-pressed="${favoriteValue(item)}" aria-label="${favoriteValue(item) ? 'Retirer des favoris' : 'Ajouter aux favoris'}" ${state.favoriteBusy.has(item.id) ? 'disabled' : ''}>${favoriteValue(item) ? '♥' : '♡'}</button><div class="variant-copy"><strong>${escapeHTML(universeLabel(item.universe))}</strong><small class="variant-kind">${isIllustrated(item) ? 'Dessin illustré' : 'Étude de découpage'}</small><button type="button" class="button button-outline variant-select" data-select-variant="${escapeHTML(item.id)}" ${page.selected_variant_id === item.id || qualityProblems(item).length ? 'disabled' : ''}>${page.selected_variant_id === item.id ? '✓ Dans mon livre' : 'Choisir ce dessin'}</button></div></article>`).join('') : `<p class="no-variants">${state.favoritesOnly ? 'Vos coups de cœur apparaîtront ici. Touchez le cœur d’un dessin pour le garder.' : 'Vos essais sont conservés ici, photo par photo.'}</p>`;
  const favorite = saved || (!pending ? variant : null), intentionKey = choiceKey(page.id, desired);
  const heart = favorite ? favoriteValue(favorite) : !!state.favoriteIntents.get(intentionKey);
  $('#favorite-current').setAttribute('aria-pressed', String(heart));
  $('#favorite-current').setAttribute('aria-label', heart ? 'Retirer ce dessin des favoris' : 'Garder ce dessin dans mes favoris');
  $('#favorite-current').innerHTML = `<span aria-hidden="true">${heart ? '♥' : '♡'}</span>`;
  $('#favorite-current').disabled = page.pending || (!favorite && !pending) || (favorite && state.favoriteBusy.has(favorite.id));
  $('#select-current').disabled = !!page.pending || pending || !variant || !!qualityProblems(variant).length || page.selected_variant_id === variant.id;
  $('#select-current').textContent = variant && page.selected_variant_id === variant.id ? '✓ Dans mon livre' : pending ? 'Enregistrement du dessin…' : 'Choisir pour mon livre';
  $('#favorites-only').setAttribute('aria-pressed', String(state.favoritesOnly));
  setUniverseSelection(desired); renderUniverseDescription();
  renderUniverseRail(page, desired);
  renderQuality(page, pending ? null : variant);
  if (pending && !page.pending) handle(renderInstantPreview(page, desired));
  else if (variant) primePreview(page, variant).catch(() => undefined);
}

function renderQuality(page, variant) {
  const panel = $('#qa-panel');
  panel.hidden = !variant;
  if (!variant) return;
  const warnings = warningsFor(page, variant);
  const errors = qualityProblems(variant);
  if (errors.length) {
    panel.classList.remove('good');
    panel.innerHTML = '<strong>Ce dessin doit être repris avant de pouvoir entrer dans le livre.</strong><ul>' + errors.map(error => `<li>${escapeHTML(error)}</li>`).join('') + '</ul><p>Essayez un niveau de détail plus simple ou une photo mieux cadrée.</p>';
    return;
  }
  panel.classList.toggle('good', !warnings.length);
  if (warnings.length) {
    panel.innerHTML = '<strong>Quelques points à vérifier avant de choisir cette version</strong><ul>' + warnings.map(warning => `<li>${escapeHTML(warning)}</li>`).join('') + '</ul><p>Comparez les visages, les détails fins et le cadrage avec la photo originale.</p>';
  } else {
    panel.innerHTML = '<strong>Les contrôles automatiques n’ont pas signalé de point bloquant.</strong><p>Vérifiez également la ressemblance et le confort des petites zones. Les contrôles techniques ne remplacent pas votre regard.</p>';
  }
}

// Keep camera originals for upload, while the gallery decodes small display-only
// previews. Two workers bound bitmap/canvas memory on phones with many photos.
function lightenLocalPreviews(items) {
  if (!window.createImageBitmap) return;
  let cursor = 0;
  const worker = async () => {
    while (cursor < items.length) {
      const item = items[cursor++], oldUrl = item.localUrl;
      let bitmap;
      try {
        if (item.file.size < 400 * 1024) continue;
        bitmap = await createImageBitmap(item.file, { resizeWidth: 1280, resizeQuality: 'high' });
        if (bitmap.height > 4096) continue;
        const canvas = document.createElement('canvas'); canvas.width = bitmap.width; canvas.height = bitmap.height;
        const context = canvas.getContext('2d'); context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(bitmap, 0, 0); bitmap.close(); bitmap = null;
        const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.88));
        canvas.width = 1; canvas.height = 1;
        if (!blob || blob.size >= item.file.size || state.book?.id !== item.bookId) continue;
        const pending = state.pendingPhotos.includes(item), persisted = item.persistedId && state.localPhotos.get(item.persistedId) === oldUrl;
        if (!pending && !persisted) continue;
        const nextUrl = URL.createObjectURL(blob); localObjectUrls.add(nextUrl);
        try {
          await new Promise((resolve, reject) => { const image = new Image(); image.onload = resolve; image.onerror = reject; image.src = nextUrl; });
        } catch { URL.revokeObjectURL(nextUrl); localObjectUrls.delete(nextUrl); continue; }
        if (state.book?.id !== item.bookId || (!state.pendingPhotos.includes(item) && !(item.persistedId && state.localPhotos.get(item.persistedId) === oldUrl))) {
          URL.revokeObjectURL(nextUrl); localObjectUrls.delete(nextUrl); continue;
        }
        item.localUrl = nextUrl;
        if (item.persistedId && state.localPhotos.get(item.persistedId) === oldUrl) state.localPhotos.set(item.persistedId, nextUrl);
        renderPhotos();
        requestAnimationFrame(() => { URL.revokeObjectURL(oldUrl); localObjectUrls.delete(oldUrl); });
      } catch { /* A display optimization never interrupts an original upload. */ }
      finally { bitmap?.close(); }
    }
  };
  worker(); worker();
}

async function uploadPhotos(files) {
  if (!state.book) { await goTo(1); return; }
  if (state.uploading) return;
  const remaining = state.book.target_photos - state.book.pages.length;
  const candidates = [...files], valid = candidates.filter(file => ['image/jpeg', 'image/png', 'image/webp'].includes(file.type) && file.size <= 20 * 1024 * 1024);
  if (valid.length !== candidates.length) toast('Choisissez des photos JPEG, PNG ou WebP de moins de 20 Mo.', true);
  const chosen = valid.slice(0, Math.max(0, remaining)); if (!chosen.length) return;
  if (valid.length > remaining) toast(`Les ${remaining} premières photos seront ajoutées au livre.`);
  const bookId = state.book.id, status = $('#upload-progress');
  const pending = chosen.map((file, index) => {
    const localUrl = URL.createObjectURL(file); localObjectUrls.add(localUrl);
    return { id: `pending-${Date.now()}-${index}`, bookId, filename: file.name, localUrl, pending: true, variants: [], file };
  });
  state.pendingPhotos.push(...pending); if (!currentPage()) { state.pageId = pending[0].id; useNewDrawingDefaults(); }
  state.uploading = true; $('#photo-upload').disabled = true;
  status.hidden = false; status.classList.remove('error');
  status.textContent = `Vos ${chosen.length} photo${chosen.length > 1 ? 's sont visibles' : ' est visible'}. Enregistrement…`;
  renderPhotos();
  lightenLocalPreviews(pending);
  let count = 0, errors = [];
  try {
    await saveMetadata();
    if (state.catalog.direct_uploads) {
      for (let offset = 0; offset < pending.length; offset += 3) {
        if (state.book?.id !== bookId) break;
        const batch = pending.slice(offset, offset + 3);
        const prepared = await api(`/api/books/${bookId}/uploads/prepare-batch`, { method: 'POST', body: { files: batch.map(item => ({ filename: item.file.name, content_type: item.file.type, size: item.file.size })) } });
        const transferred = await Promise.all((prepared.uploads || []).map(async (upload, index) => {
          const item = batch[index]; if (!item) throw new Error('Une autorisation de transfert est incomplète.');
          const url = new URL(upload.upload_url);
          if (url.protocol !== 'https:' || url.username || url.password || url.port || !(url.hostname === 'blob.vercel-storage.com' || url.hostname.endsWith('.blob.vercel-storage.com') || (url.hostname === 'vercel.com' && (url.pathname === '/api/blob' || url.pathname.startsWith('/api/blob/'))))) throw new Error('Le transfert sécurisé de cette photo est indisponible.');
          try {
            const response = await fetch(url.href, { method: 'PUT', headers: upload.headers || { 'Content-Type': item.file.type }, body: item.file, credentials: 'omit' });
            if (!response.ok) throw new Error('Transfert interrompu.');
            return { item, pathname: upload.pathname, filename: item.filename };
          } catch { errors.push({ message: `${item.filename} : le transfert n’a pas abouti.` }); return null; }
        }));
        if ((prepared.uploads || []).length !== batch.length) throw new Error('Certaines photos n’ont pas reçu leur autorisation de transfert.');
        const successful = transferred.filter(Boolean);
        if (successful.length) {
          const result = await api(`/api/books/${bookId}/photos/import-batch`, { method: 'POST', body: { files: successful.map(({ pathname, filename }) => ({ pathname, filename })) } });
          if (state.book?.id !== bookId) break;
          const added = [...(result.added || [])], used = new Set();
          for (const transfer of successful) {
            const page = added.find(value => !used.has(value.id) && value.filename === transfer.filename);
            if (!page) continue;
            used.add(page.id); count += 1;
            transfer.item.persistedId = page.id;
            state.localPhotos.set(page.id, transfer.item.localUrl);
            if (state.pageId === transfer.item.id) state.pageId = page.id;
            const desired = state.photoUniverses.get(transfer.item.id); if (desired) state.photoUniverses.set(page.id, desired);
            state.pendingPhotos = state.pendingPhotos.filter(value => value.id !== transfer.item.id);
          }
          errors.push(...(result.errors || []));
          setBook(result.book);
        }
        status.textContent = `${count} / ${chosen.length} photos enregistrées. Vous pouvez déjà les parcourir.`;
      }
    } else {
      const data = new FormData(); chosen.forEach(file => data.append('files', file));
      const result = await api(`/api/books/${bookId}/photos`, { method: 'POST', body: data });
      if (state.book?.id !== bookId) return;
      const used = new Set();
      for (const item of pending) {
        const page = (result.added || []).find(value => !used.has(value.id) && value.filename === item.filename);
        if (!page) continue;
        used.add(page.id); item.persistedId = page.id; state.localPhotos.set(page.id, item.localUrl); if (state.pageId === item.id) state.pageId = page.id;
        state.pendingPhotos = state.pendingPhotos.filter(value => value.id !== item.id);
      }
      count = result.added?.length || 0; errors.push(...(result.errors || [])); setBook(result.book);
    }
  } catch (error) {
    errors.push({ message: error.message });
    // An import can have committed before a response was lost. Read the book,
    // never replay the import or other state-changing request automatically.
    try { if (state.book?.id === bookId) setBook(await api(`/api/books/${bookId}`)); } catch { /* Explicit retry remains available. */ }
  } finally {
    const left = state.pendingPhotos.filter(item => item.bookId === bookId);
    for (const item of left) { URL.revokeObjectURL(item.localUrl); localObjectUrls.delete(item.localUrl); }
    state.pendingPhotos = state.pendingPhotos.filter(item => item.bookId !== bookId);
    state.uploading = false; $('#photo-upload').disabled = false; $('#photo-upload').value = '';
    if (state.book?.id === bookId) {
      if (!currentPage()) state.pageId = state.book.pages?.[0]?.id || null;
      status.classList.toggle('error', !!errors.length);
      status.textContent = `${count} photo${count > 1 ? 's enregistrées' : ' enregistrée'}.${errors.length ? ` ${errors.map(messageOf).join(' · ')} Vous pouvez ajouter à nouveau les photos manquantes.` : ' Touchez un univers pour découvrir votre dessin.'}`;
      renderPhotos(); if (count) setSaveStatus('Vos photos sont enregistrées');
      const page = currentPage();
      if (page && !page.variants?.length && count && !errors.length) requestUniverse(state.photoUniverses.get(page.id) || 'original');
      else drainGenerationQueue();
    }
  }
}

async function generateIllustration() { requestUniverse($('#universe-select').value || 'original', { force: true }); }

function renderJob() {
  const job = state.job;
  $('#generation-progress').hidden = !job;
  if (!job) return;
  const here = job.pageId === state.pageId;
  $('#generation-message').textContent = `${here ? '' : 'Une autre photo se prépare. '}${job.stage || 'Votre dessin prend forme…'}${state.queuedGeneration ? ' Votre dernier choix sera appliqué ensuite.' : ''}`;
  const seconds = Math.max(0, Math.floor((Date.now() - (job.startedAt || Date.now())) / 1000));
  const elapsed = seconds ? ` · ${seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, '0')} s`}` : '';
  const cachedScene = sameSceneVariant(state.book?.pages?.find(page => page.id === job.pageId), job.universe, job.config);
  $('#generation-detail').textContent = `${cachedScene ? 'Le dessin existant est réutilisé pour ce nouvel essai.' : job.config?.artwork_mode === 'premium' ? 'Le service illustre votre photo, puis les zones et les codes sont préparés.' : 'Analyse des zones et préparation des codes.'} Vous pouvez parcourir les autres photos${elapsed}.`;
  if (job.progress === null || job.progress === undefined) $('#job-progress').removeAttribute('value');
  else $('#job-progress').value = Number(job.progress || 0);
}

async function pollJob() {
  while (state.job) {
    const pending = state.job;
    if (pending.status === 'succeeded') {
      state.job = null; renderJob();
      const book = await api(`/api/books/${pending.bookId}`);
      if (state.book?.id === pending.bookId) {
        const page = book.pages.find(value => value.id === pending.pageId);
        const variant = page?.variants?.find(value => value.id === pending.result?.variant_id) || findMatchingVariant(page, pending.universe, pending.config);
        if (variant && state.pageId === pending.pageId && state.choiceSequences.get(page.id) === pending.choiceSeq) state.variantId = variant.id;
        setBook(book);
        if (variant) await flushFavoriteIntent(page, variant, pending.config);
      }
      return;
    }
    if (pending.status === 'failed') { state.job = null; renderJob(); throw new Error(pending.error || 'Le dessin n’a pas pu être enregistré. Touchez à nouveau l’univers pour réessayer.'); }
    await new Promise(resolve => window.setTimeout(resolve, 1000));
    const job = await api(`/api/jobs/${pending.id}`);
    state.job = { ...pending, ...job }; renderJob();
  }
}

async function chooseVariant(id) {
  const page = currentPage(), variant = page?.variants?.find(item => item.id === id);
  if (!variant || !state.book) return;
  if (qualityProblems(variant).length) { toast('Ce dessin comporte un contrôle bloquant. Créez un autre essai.', true); return; }
  const warnings = warningsFor(page, variant);
  let accepted = false;
  if (warnings.length) {
    accepted = await confirmChoice('Un dernier regard sur cette version', [
      ...warnings,
      'En choisissant cette version, vous confirmez avoir examiné ces points, le cadrage et la ressemblance avec votre photo.',
    ], 'J’ai vérifié, choisir cette version', 'POINTS DE VIGILANCE');
    if (!accepted) return;
  }
  const bookId = state.book.id;
  const book = await api(`/api/books/${bookId}/photos/${page.id}/variants/${id}/select`, { method: 'POST', body: { accept_warnings: accepted } });
  if (state.book?.id !== bookId) return;
  if (state.pageId === page.id) state.variantId = id;
  setBook(book);
  toast('Cette illustration a trouvé sa place dans votre livre.');
}

function renderComposition() {
  const pages = state.book?.pages || [], count = validatedCount(), target = state.book?.target_photos || 8;
  const complete = count === target && pages.length === target;
  $('#composition-summary').classList.toggle('warning', !complete);
  $('#composition-summary').innerHTML = `<span><strong>${count} / ${target} illustrations choisies</strong> · ${pages.length} photo${pages.length > 1 ? 's' : ''} présente${pages.length > 1 ? 's' : ''}</span>${complete ? '<span>Votre histoire est prête à être composée.</span>' : '<button class="text-link" type="button" data-go="2">Compléter mes souvenirs →</button>'}`;
  $('#page-order').innerHTML = pages.length ? pages.map((page, index) => {
    const variant = selectedVariant(page);
    return `<article class="order-card" draggable="true" data-order-page="${escapeHTML(page.id)}"><span class="drag-handle" aria-hidden="true">⠿</span><span class="order-position">${String(index + 1).padStart(2, '0')}</span><img src="${escapeHTML(variant ? variantUrl(variant, 'mystery') : thumbnailUrl(page))}" alt="${variant ? 'Page numérotée' : 'Photo en attente'}" loading="lazy"><div class="order-copy"><h3>${escapeHTML(page.filename || 'Votre souvenir')}</h3><p class="${variant ? '' : 'missing'}">${variant ? `✓ ${escapeHTML(universeLabel(variant.universe))} · Version choisie` : 'Une illustration reste à choisir'}</p></div><div class="order-actions"><button class="button button-quiet order-edit" data-edit-photo="${escapeHTML(page.id)}" type="button">${variant ? 'Revoir' : 'Choisir une version'} ↗</button><button class="button button-outline" type="button" data-move-photo="${escapeHTML(page.id)}" data-direction="-1" aria-label="Monter l’illustration ${index + 1}" ${index === 0 ? 'disabled' : ''}>↑</button><button class="button button-outline" type="button" data-move-photo="${escapeHTML(page.id)}" data-direction="1" aria-label="Descendre l’illustration ${index + 1}" ${index === pages.length - 1 ? 'disabled' : ''}>↓</button></div></article>`;
  }).join('') : '<div class="empty-state"><h3>Votre livre attend ses premières pages.</h3><p>Ajoutez vos photos à l’étape « Les souvenirs ».</p></div>';
}

async function reorderPages(ids) {
  const book = await api(`/api/books/${state.book.id}/pages/reorder`, { method: 'POST', body: { page_ids: ids } });
  setBook(book); setSaveStatus('L’ordre de vos pages est enregistré');
}

function renderGift() {
  const book = state.book;
  if (!book) return;
  const template = state.catalog.templates.find(item => item.key === book.template);
  $('#gift-book').className = `gift-book ${['heritage', 'nocturne', 'botanique'].includes(book.template) ? book.template : 'heritage'}`;
  $('#gift-title').textContent = book.title;
  $('#gift-recipient').textContent = book.recipient ? `Pour ${book.recipient}` : 'UN LIVRE RIEN QUE POUR VOUS';
  $('#gift-summary-title').textContent = book.title;
  $('#gift-dedication').textContent = book.dedication || 'Un mot personnel rendra ce cadeau encore plus précieux.';
  const count = validatedCount();
  $('#gift-facts').innerHTML = [
    ['Collection', template?.label || book.template], ['Destinataire', book.recipient || 'À personnaliser si vous le souhaitez'],
    ['Illustrations choisies', `${count} / ${book.target_photos}`], ['Format intérieur', 'A4 · Portrait'], ['Solutions', 'Document séparé'],
  ].map(([term, value]) => `<div><dt>${escapeHTML(term)}</dt><dd>${escapeHTML(value)}</dd></div>`).join('');
  const first = book.pages?.map(selectedVariant).find(Boolean);
  $('.exported-cover')?.remove();
  $('#gift-cover-picture').innerHTML = first ? `<img src="${escapeHTML(variantUrl(first, 'solution_final'))}" alt="Illustration de présentation du cadeau">` : '<span aria-hidden="true">✧</span>';
  renderReadiness(); renderDownloads(); updateExportButton();
}

function renderReadiness() {
  const book = state.book;
  if (!book) return;
  const pages = book.pages || [], count = validatedCount();
  const problems = book.readiness?.problems || [];
  const checks = [
    [pages.length === book.target_photos, `${pages.length} photo${pages.length > 1 ? 's' : ''} sur ${book.target_photos} prévues`],
    [count === book.target_photos, `${count} illustration${count > 1 ? 's' : ''} validée${count > 1 ? 's' : ''}`],
    [!!book.title?.trim(), 'Le titre du livre est renseigné'],
  ];
  $('#readiness-checklist').innerHTML = checks.map(([ok, label]) => `<div class="readiness-item${ok ? '' : ' bad'}"><span aria-hidden="true">${ok ? '✓' : '○'}</span><span>${escapeHTML(label)}</span></div>`).join('') + problems.map(problem => `<div class="readiness-item bad"><span aria-hidden="true">!</span><span>${escapeHTML(messageOf(problem))}</span></div>`).join('');
  $('#proof-button').disabled = !pages.some(page => page.selected_variant_id);
}

async function refreshReadiness() {
  if (!state.book) return;
  const id = state.book.id;
  const readiness = await api(`/api/books/${id}/readiness`);
  if (state.book?.id !== id) return;
  state.book.readiness = readiness;
  renderReadiness(); updateExportButton();
  if (!state.export) {
    try {
      const result = await api(`/api/books/${id}/exports`);
      if (state.book?.id === id && result.revision === state.book.revision) { state.export = result; renderDownloads(); }
    } catch (error) { if (error.status !== 404) console.warn('Aucun export récupérable pour cette version.'); }
  }
}

function updateExportButton() {
  $('#export-button').disabled = !state.book?.readiness?.ready || !$('#approval-checkbox').checked || state.busy || !!state.job || state.metadataDirty;
}

function renderDownloads() {
  const result = state.export;
  const container = $('#export-downloads');
  container.hidden = !result; $('#print-request-panel').hidden = !result;
  if (!result) { container.replaceChildren(); return; }
  const entries = [
    ['archive', 'Tout le dossier', 'Archive ZIP · fichiers et récapitulatif', '↓'],
    ['interior_pdf', 'Le livre à colorier', 'PDF intérieur · pages et nuanciers', '◇'],
    ['solutions_pdf', 'Les solutions', 'PDF séparé · à garder pour la surprise', '✧'],
    ['cover_pdf', 'La couverture', 'PDF de couverture · format à plat', '▤'],
    ['guides_pdf', 'Guides des petits détails', 'PDF séparé · codes agrandis', '⌕'],
    ['manifest', 'Le récapitulatif', 'Version et contrôle des fichiers', '✓'],
  ];
  container.innerHTML = entries.filter(([key]) => safeUrl(result.files?.[key])).map(([key, title, subtitle, icon]) => `<a class="download-card" href="${escapeHTML(safeUrl(result.files[key]))}" download><span aria-hidden="true">${icon}</span><div><strong>${escapeHTML(title)}</strong><small>${escapeHTML(subtitle)}</small></div></a>`).join('');
  const preview = safeUrl(result.files?.cover_preview);
  if (preview) {
    $('.exported-cover')?.remove();
    const image = document.createElement('img');
    image.className = 'exported-cover'; image.src = preview; image.alt = 'Couverture exportée de votre livre';
    $('#gift-book').append(image);
  }
}

async function exportBook() {
  if (!state.book || state.busy) return;
  await saveMetadata();
  if (!$('#approval-checkbox').checked) { toast('Vérifiez votre livre et confirmez cette version avant de générer les documents.'); return; }
  state.busy = true; updateExportButton();
  const status = $('#export-status'); status.hidden = false; status.classList.remove('error');
  status.textContent = 'Votre livre se prépare : couverture, pages à colorier et solutions séparées…';
  const id = state.book.id, revision = state.book.revision;
  try {
    const result = await api(`/api/books/${id}/exports`, { method: 'POST', body: { approved_revision: revision } });
    if (state.book?.id !== id) return;
    if (state.book.revision !== revision) throw new Error('Votre livre a changé pendant la préparation. Vérifiez la nouvelle version avant de relancer l’export.');
    state.export = result;
    status.textContent = 'Votre dossier est prêt. Téléchargez les PDF et examinez un essai papier avant de demander leur impression.';
    if (result.warnings?.length) status.textContent += ` ${result.warnings.map(messageOf).join(' ')}`;
    renderDownloads(); renderProgress();
    toast('Votre livre et ses documents sont prêts à être téléchargés.');
  } catch (error) { status.classList.add('error'); status.textContent = error.message; }
  finally { state.busy = false; updateExportButton(); }
}

function proofPages() { return state.book?.pages?.filter(page => selectedVariant(page)) || []; }
function renderProof() {
  const pages = proofPages();
  if (!pages.length) return;
  state.proofIndex = Math.max(0, Math.min(state.proofIndex, pages.length - 1));
  const page = pages[state.proofIndex], variant = selectedVariant(page);
  $('#proof-index').textContent = `ILLUSTRATION ${state.proofIndex + 1} / ${pages.length}`;
  $('#proof-title').textContent = page.filename || 'Votre souvenir';
  $('#proof-source').src = photoUrl(page);
  $('#proof-solution').src = variantUrl(variant, 'solution_final');
  $('#proof-mystery').src = variantUrl(variant, 'mystery');
  $('#proof-prev').disabled = state.proofIndex === 0;
  $('#proof-next').disabled = state.proofIndex === pages.length - 1;
}

function openProof(index = 0) {
  if (!proofPages().length) { toast('Choisissez d’abord une illustration pour votre livre.'); return; }
  state.proofIndex = index; renderProof(); $('#proof-dialog').showModal();
}

function handle(promise) { Promise.resolve(promise).catch(error => toast(error.message || 'Une erreur est survenue. Vous pouvez réessayer.', true, 10000)); }

function bindEvents() {
  $('#photo-workspace').addEventListener('error', event => {
    if (event.target.tagName === 'IMG' && event.target.getAttribute('src')) refreshAssetUrls();
  }, true);
  $('#book-form').addEventListener('input', scheduleSave);
  $('#book-form').addEventListener('change', scheduleSave);
  $('#book-form').addEventListener('submit', event => {
    event.preventDefault();
    handle((async () => {
      if (!$('#book-form').reportValidity()) return;
      const button = $('#create-book'); button.disabled = true;
      try {
        if (state.book) await saveMetadata();
        else {
          const book = await api('/api/books', { method: 'POST', body: formPayload() });
          setBook(book, { fillForm: true }); setSaveStatus('Votre livre est enregistré');
        }
        await goTo(2);
      } finally { button.disabled = false; }
    })());
  });
  document.addEventListener('click', event => {
    const go = event.target.closest('[data-go]');
    if (go) handle(goTo(Number(go.dataset.go)));
    const step = event.target.closest('.step[data-step]');
    if (step) handle(goTo(Number(step.dataset.step)));
  });
  $('#project-select').addEventListener('change', event => {
    if (event.target.value) handle(openBook(event.target.value));
    else handle(startNewBook());
  });
  $('#new-book').addEventListener('click', () => handle(startNewBook()));
  const upload = $('#upload-zone'), input = $('#photo-upload');
  upload.addEventListener('click', () => input.click());
  upload.addEventListener('keydown', event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); input.click(); } });
  ['dragenter', 'dragover'].forEach(name => upload.addEventListener(name, event => { event.preventDefault(); upload.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach(name => upload.addEventListener(name, event => { event.preventDefault(); upload.classList.remove('dragging'); }));
  upload.addEventListener('drop', event => handle(uploadPhotos(event.dataTransfer.files)));
  input.addEventListener('change', () => handle(uploadPhotos(input.files)));
  $('#photo-strip').addEventListener('click', event => {
    const button = event.target.closest('[data-photo]');
    if (!button) return;
    selectPhoto(button.dataset.photo);
  });
  $$('.result-tabs [data-view]').forEach(button => button.addEventListener('click', () => {
    state.view = button.dataset.view;
    $$('.result-tabs [data-view]').forEach(item => { item.setAttribute('aria-selected', String(item === button)); item.tabIndex = item === button ? 0 : -1; });
    $('#result-stage').setAttribute('aria-labelledby', button.id);
    renderPhotos();
  }));
  $$('.result-tabs [data-view]').filter(button => !button.hidden).forEach((button, index, buttons) => {
    button.tabIndex = index === 0 ? 0 : -1;
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus(); buttons[next].click();
    });
  });
  $('#generate-button').addEventListener('click', () => handle(generateIllustration()));
  $('#create-premium-button').addEventListener('click', () => {
    $('#artwork-mode').value = 'premium';
    requestUniverse(desiredUniverse(currentPage()));
  });
  $('#artwork-mode').addEventListener('change', () => renderPhotos());
  $('#universe-select').addEventListener('change', () => requestUniverse($('#universe-select').value));
  $('#universe-rail').addEventListener('click', event => { const button = event.target.closest('[data-universe]'); if (button) requestUniverse(button.dataset.universe); });
  $$('[data-universe-tab]').forEach((button, index, buttons) => {
    button.addEventListener('click', () => {
      state.universeTab = button.dataset.universeTab; state.universeNotice = '';
      renderUniverseRail(currentPage(), currentPage() ? desiredUniverse(currentPage()) : 'original'); renderUniverseDescription();
    });
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus(); buttons[next].click();
    });
  });
  $('#prev-photo').addEventListener('click', () => movePhoto(-1));
  $('#next-photo').addEventListener('click', () => movePhoto(1));
  $('#favorite-current').addEventListener('click', () => handle(toggleCurrentFavorite()));
  $('#select-current').addEventListener('click', () => { const variant = activeVariant(); if (variant && !$('#select-current').disabled) handle(chooseVariant(variant.id)); });
  $('#favorites-only').addEventListener('click', () => { state.favoritesOnly = !state.favoritesOnly; renderPhotos(); });
  document.addEventListener('keydown', event => {
    if (state.step !== 2 || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || document.querySelector('dialog[open]') || event.target.closest('input,textarea,select,[contenteditable=true]')) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); movePhoto(event.key === 'ArrowRight' ? 1 : -1); }
  });
  $('#variants-grid').addEventListener('click', event => {
    const favorite = event.target.closest('[data-favorite]');
    if (favorite) {
      const page = currentPage(), variant = page?.variants?.find(item => item.id === favorite.dataset.favorite);
      if (variant) handle(toggleFavorite(page, variant));
      return;
    }
    const select = event.target.closest('[data-select-variant]');
    if (select) { select.disabled = true; handle(chooseVariant(select.dataset.selectVariant).finally(() => { select.disabled = false; })); return; }
    const preview = event.target.closest('[data-variant]');
    if (preview) {
      const variant = currentPage()?.variants?.find(item => item.id === preview.dataset.variant);
      if (variant) { adoptVariantSettings(variant); state.variantId = variant.id; state.photoUniverses.set(state.pageId, variant.universe); state.choiceSequences.set(state.pageId, ++choiceSequence); renderPhotos(); }
    }
  });
  $('#remove-photo').addEventListener('click', () => handle((async () => {
    const page = currentPage(); if (!page) return;
    if (!await confirmChoice('Retirer ce souvenir ?', 'La photo, ses essais et sa version sélectionnée seront retirés de ce livre.', 'Retirer la photo')) return;
    setBook(await api(`/api/books/${state.book.id}/photos/${page.id}`, { method: 'DELETE' }));
    toast('La photo a été retirée du livre.');
  })()));
  $('#page-order').addEventListener('click', event => {
    const edit = event.target.closest('[data-edit-photo]');
    if (edit) { state.pageId = edit.dataset.editPhoto; state.variantId = null; renderPhotos(); handle(goTo(2)); return; }
    const move = event.target.closest('[data-move-photo]');
    if (!move) return;
    const ids = state.book.pages.map(page => page.id), from = ids.indexOf(move.dataset.movePhoto), to = from + Number(move.dataset.direction);
    if (to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]];
    handle(reorderPages(ids));
  });
  $('#page-order').addEventListener('dragstart', event => {
    if (event.target.closest('button')) { event.preventDefault(); return; }
    const card = event.target.closest('[data-order-page]');
    if (!card) return;
    state.draggedPage = card.dataset.orderPage;
    event.dataTransfer.setData('text/plain', state.draggedPage); event.dataTransfer.effectAllowed = 'move';
  });
  $('#page-order').addEventListener('dragover', event => {
    const card = event.target.closest('[data-order-page]');
    if (!card || !state.draggedPage) return;
    event.preventDefault(); card.classList.add('drag-over');
  });
  $('#page-order').addEventListener('dragleave', event => event.target.closest('[data-order-page]')?.classList.remove('drag-over'));
  $('#page-order').addEventListener('dragend', () => { state.draggedPage = null; $$('.order-card').forEach(card => card.classList.remove('drag-over')); });
  $('#page-order').addEventListener('drop', event => {
    const card = event.target.closest('[data-order-page]');
    if (!card || !state.draggedPage) return;
    event.preventDefault();
    const ids = state.book.pages.map(page => page.id), from = ids.indexOf(state.draggedPage), to = ids.indexOf(card.dataset.orderPage);
    state.draggedPage = null;
    if (from < 0 || to < 0 || from === to) return;
    const [id] = ids.splice(from, 1); ids.splice(to, 0, id); handle(reorderPages(ids));
  });
  $('#approval-checkbox').addEventListener('change', updateExportButton);
  $('#export-button').addEventListener('click', () => handle(exportBook()));
  $('#proof-button').addEventListener('click', () => openProof());
  $('#enlarge-result').addEventListener('click', () => {
    const index = proofPages().findIndex(page => page.id === state.pageId);
    if (index < 0) { toast('Choisissez cette version pour le livre afin de la retrouver dans l’aperçu complet.'); return; }
    openProof(index);
  });
  $('#proof-prev').addEventListener('click', () => { state.proofIndex -= 1; renderProof(); });
  $('#proof-next').addEventListener('click', () => { state.proofIndex += 1; renderProof(); });
  $('#proof-dialog').addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft' && !$('#proof-prev').disabled) { state.proofIndex -= 1; renderProof(); }
    if (event.key === 'ArrowRight' && !$('#proof-next').disabled) { state.proofIndex += 1; renderProof(); }
  });
  $('#proof-close').addEventListener('click', () => $('#proof-dialog').close());
  $('#help-button').addEventListener('click', () => $('#help-dialog').showModal());
  $('#help-close').addEventListener('click', () => $('#help-dialog').close());
  $('#print-request-button').addEventListener('click', () => handle((async () => {
    if (!state.export || !state.book) return;
    const button = $('#print-request-button'); button.disabled = true;
    try {
      const response = await api(`/api/books/${state.book.id}/print-requests`, { method: 'POST', body: { approved_revision: state.book.revision, export_id: state.export.id } });
      toast(response.message || 'Votre demande est préparée localement. Aucun paiement ni envoi à un imprimeur n’a été effectué.', false, 12000);
    } finally { button.disabled = false; }
  })()));
  window.addEventListener('beforeunload', event => {
    if (!state.metadataDirty && !state.metadataSaving && !state.uploading && !state.job && !state.queuedGeneration && !state.pinnedGenerations.size) return;
    event.preventDefault(); event.returnValue = '';
  });
}

async function startNewBook() {
  if (hasPendingFavoriteGeneration()) { renderProjects(); toast('Votre coup de cœur s’enregistre. Vous pouvez parcourir vos photos pendant ce temps.'); return; }
  if (state.uploading) { renderProjects(); toast('Laissez les photos terminer leur enregistrement avant de changer de livre.'); return; }
  if (state.metadataDirty) await saveMetadata();
  bookOpenSequence += 1; releaseBookObjectUrls();
  state.book = null; state.pageId = null; state.variantId = null; state.export = null; state.metadataDirty = false; state.queuedGeneration = null; state.pinnedGenerations.clear();
  fillBookForm(null); useNewDrawingDefaults(); renderProjects(); renderProgress();
  try { localStorage.removeItem('colorart.activeBook'); } catch { /* Stockage facultatif. */ }
  setSaveStatus('Un nouveau livre, une nouvelle histoire');
  await goTo(1);
}

function renderUniverseDescription() {
  const key = $('#universe-select').value || 'original', { art, palette } = universeParts(key);
  const artDescription = universeItem(art)?.description || 'Votre photo reste la référence pour les visages, les poses et les souvenirs.';
  const paletteDescription = universeItem(palette)?.description || '';
  $('#universe-description').textContent = state.universeNotice || (state.universeTab === 'palette'
    ? `${art === 'original' ? 'Fidèle à la photo' : universeLabel(art)} · ${palette === 'original' ? 'Couleurs d’origine.' : paletteDescription}`
    : `${artDescription}${palette === 'original' ? '' : ` Couleurs : ${universeLabel(palette)}.`}`);
  const available = !!(state.catalog.artistic_enabled ?? state.catalog.premium_enabled);
  $('#artwork-mode-note').textContent = available
    ? 'Chaque univers artistique crée un nouveau dessin et son décor. Comparez les visages et l’expression avant de choisir votre page.'
    : 'La création de nouveaux dessins illustrés nécessite l’activation du service. Vos dessins enregistrés, leurs couleurs et les études de découpage restent disponibles.';
  $('#artwork-mode-note').classList.toggle('service-unavailable', !available);
}

async function init() {
  bindEvents();
  try {
    const [catalog, projects] = await Promise.all([api('/api/catalog'), api('/api/books')]);
    state.catalog = catalog; state.books = projects.books || [];
    const premium = !!catalog.premium_enabled;
    $('#artwork-mode option[value="premium"]').disabled = !premium;
    useNewDrawingDefaults();
    $('#universe-select').innerHTML = (catalog.universes || []).map(item => `<option value="${escapeHTML(item.key)}">${escapeHTML(item.label)}</option>`).join('');
    renderUniverseDescription(); renderProjects(); renderProgress(); renderUniverseRail(null, 'original');
    let active;
    try { active = localStorage.getItem('colorart.activeBook'); } catch { /* Atelier utilisable sans stockage navigateur. */ }
    if (active && state.books.some(book => book.id === active)) await openBook(active);
    else setSaveStatus('Votre atelier est prêt');
  } catch (error) {
    setSaveStatus('Connexion à l’atelier à vérifier', 'error');
    toast(error.message, true, 0);
  }
}

init();
