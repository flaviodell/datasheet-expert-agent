// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    // Clear any browser-cached file selection to avoid ghost uploads on reload.
    document.getElementById('pdf-input').value = '';

    populateDocumentSelector();

    document.getElementById('upload-btn').addEventListener('click', uploadFile);
    document.getElementById('send-btn').addEventListener('click', sendMessage);
    document.getElementById('user-input').addEventListener('keydown', handleTextareaKey);

    // Poll server health every 3 seconds to keep the UI status indicator current.
    checkServerStatus();
    setInterval(checkServerStatus, 3000);
});


// ---------------------------------------------------------------------------
// Server health check
// ---------------------------------------------------------------------------

async function checkServerStatus() {
    const statusLabel = document.getElementById('status-text');
    try {
        const res = await fetch('/health', { cache: 'no-cache' });
        const online = res.ok;
        statusLabel.innerText = online ? 'Online' : 'Offline';
        statusLabel.style.color = online ? 'white' : '#ffcdd2';
        setInterfaceEnabled(online);
    } catch {
        statusLabel.innerText = 'Offline';
        statusLabel.style.color = '#ffcdd2';
        setInterfaceEnabled(false);
    }
}

function setInterfaceEnabled(enabled) {
    ['send-btn', 'user-input', 'pdf-input', 'upload-btn', 'doc-selector'].forEach(id => {
        document.getElementById(id).disabled = !enabled;
    });
    if (!enabled) {
        document.getElementById('user-input').placeholder = 'Connessione persa...';
    }
}


// ---------------------------------------------------------------------------
// Document selector
// ---------------------------------------------------------------------------

// Reads the file list injected by Flask at render time and populates the
// selector. Avoids a round-trip API call on page load.
function populateDocumentSelector() {
    const bridge = document.getElementById('data-bridge');
    if (!bridge) return;

    try {
        const files = JSON.parse(bridge.getAttribute('data-files'));
        if (files && files.length > 0) updateDocumentSelector(files);
    } catch (e) {
        console.error('Failed to parse initial file list:', e);
    }
}

function updateDocumentSelector(files) {
    const select = document.getElementById('doc-selector');
    select.innerHTML = '';
    files.forEach(f => {
        const opt = document.createElement('option');
        opt.value = opt.textContent = f;
        select.appendChild(opt);
    });
    // Auto-select the most recently uploaded document.
    select.value = files[files.length - 1];
}


// ---------------------------------------------------------------------------
// File upload
// ---------------------------------------------------------------------------

async function uploadFile() {
    const input  = document.getElementById('pdf-input');
    const loader = document.getElementById('loader-wrapper');
    const btn    = document.getElementById('upload-btn');

    if (!input.files[0]) return;

    btn.disabled = true;
    loader.style.display = 'flex';

    const formData = new FormData();
    formData.append('file', input.files[0]);

    try {
        const res  = await fetch('/upload', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.status === 'success') updateDocumentSelector(data.files);
    } catch (e) {
        console.error('Upload error:', e);
    } finally {
        btn.disabled = false;
        loader.style.display = 'none';
    }
}


// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

async function sendMessage() {
    const input   = document.getElementById('user-input');
    const chatWin = document.getElementById('chat-window');
    const typing  = document.getElementById('typing-indicator');
    const doc     = document.getElementById('doc-selector').value;
    const text    = input.value.trim();

    if (!text) return;

    if (!doc) {
        appendSystemMessage('⚠️ Carica e seleziona un documento prima di inviare una domanda.');
        return;
    }

    appendMessage('user', text);
    input.value = '';
    input.style.height = '45px';
    typing.classList.add('active');
    chatWin.scrollTop = chatWin.scrollHeight;

    try {
        const res  = await fetch('/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text, document: doc }),
        });
        const data = await res.json();
        appendMessage('ai', data.response);
    } catch (e) {
        appendMessage('ai', 'Errore di rete. Riprova.');
        console.error('Send error:', e);
    } finally {
        typing.classList.remove('active');
        chatWin.scrollTop = chatWin.scrollHeight;
    }
}

function appendMessage(role, text) {
    const chatWin = document.getElementById('chat-window');
    const typing  = document.getElementById('typing-indicator');
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    div.innerText = text;
    chatWin.insertBefore(div, typing);
}

function appendSystemMessage(text) {
    const chatWin = document.getElementById('chat-window');
    const typing  = document.getElementById('typing-indicator');
    const div = document.createElement('div');
    div.className = 'system-msg';
    div.innerText = text;
    chatWin.insertBefore(div, typing);
    chatWin.scrollTop = chatWin.scrollHeight;
}


// ---------------------------------------------------------------------------
// Textarea auto-resize
// ---------------------------------------------------------------------------

function handleTextareaKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
        e.target.style.height = '45px';
        return;
    }
    e.target.style.height = '45px';
    if (e.target.scrollHeight > 45) {
        e.target.style.height = e.target.scrollHeight + 'px';
        e.target.style.overflowY = 'auto';
    } else {
        e.target.style.overflowY = 'hidden';
    }
}
