const form = document.querySelector('#downloadForm');
const urlInput = document.querySelector('#url');
const previewBtn = document.querySelector('#previewBtn');
const downloadBtn = document.querySelector('#downloadBtn');
const message = document.querySelector('#message');
const preview = document.querySelector('#preview');
const thumb = document.querySelector('#thumb');
const title = document.querySelector('#title');
const channel = document.querySelector('#channel');
const duration = document.querySelector('#duration');

document.querySelectorAll('.format').forEach(card => {
  card.addEventListener('click', () => {
    document.querySelectorAll('.format').forEach(x => x.classList.remove('active'));
    card.classList.add('active');
  });
});

function showMessage(text, type = 'error') {
  message.textContent = text;
  message.className = `message ${type}`;
}
function clearMessage() { message.className = 'message hidden'; message.textContent = ''; }
function setLoading(on) { downloadBtn.classList.toggle('loading', on); downloadBtn.disabled = on; previewBtn.disabled = on; }

async function analyse() {
  clearMessage();
  const url = urlInput.value.trim();
  if (!url) return showMessage('Cola primeiro o link do vídeo.');
  previewBtn.textContent = 'A analisar…'; previewBtn.disabled = true;
  try {
    const res = await fetch('/api/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Não foi possível analisar o vídeo.');
    thumb.src = data.video.thumbnail || '';
    title.textContent = data.video.title || 'Vídeo do YouTube';
    channel.textContent = data.video.channel || 'YouTube';
    duration.textContent = data.video.duration || '';
    preview.classList.remove('hidden');
    showMessage('Vídeo validado. Escolhe a qualidade e inicia o download.', 'ok');
  } catch (e) {
    preview.classList.add('hidden'); showMessage(e.message);
  } finally { previewBtn.textContent = 'Analisar'; previewBtn.disabled = false; }
}
previewBtn.addEventListener('click', analyse);

form.addEventListener('submit', async (event) => {
  event.preventDefault(); clearMessage(); setLoading(true);
  showMessage('A obter e processar o ficheiro. Mantém esta página aberta…', 'info');
  try {
    const data = new FormData(form);
    const res = await fetch('/api/download', {method:'POST', body:data});
    if (!res.ok) {
      let err = 'Não foi possível concluir o download.';
      try { const body = await res.json(); err = body.error || err; } catch (_) {}
      throw new Error(err);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match ? match[1] : 'youtube_download';
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = href; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 2000);
    showMessage('Ficheiro preparado. O download para o computador foi iniciado.', 'ok');
  } catch (e) { showMessage(e.message); }
  finally { setLoading(false); }
});
