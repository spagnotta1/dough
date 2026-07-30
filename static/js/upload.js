(function () {
    var dropZone = document.getElementById('dropZone');
    var fileInput = document.getElementById('files');
    var form      = document.getElementById('uploadForm');
    var preview   = document.getElementById('previewSection');
    var previewTitle = document.getElementById('previewTitle');
    var previewBody  = document.getElementById('previewBody');
    var fileLabel    = document.getElementById('fileLabel');
    var uploadBtn    = document.getElementById('uploadBtn');
    var uploadStatus = document.getElementById('uploadStatus');

    // ── Drag-and-drop ─────────────────────────────────────────────────────────
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(function(ev) {
        dropZone.addEventListener(ev, function(e) { e.preventDefault(); e.stopPropagation(); });
    });
    ['dragenter', 'dragover'].forEach(function(ev) {
        dropZone.addEventListener(ev, function() { dropZone.classList.add('upload-dropzone--active'); });
    });
    ['dragleave', 'drop'].forEach(function(ev) {
        dropZone.addEventListener(ev, function() { dropZone.classList.remove('upload-dropzone--active'); });
    });
    dropZone.addEventListener('drop', function(e) {
        fileInput.files = e.dataTransfer.files;
        handleFiles(fileInput.files);
    });
    dropZone.addEventListener('click', function() { fileInput.click(); });
    fileInput.addEventListener('change', function() { handleFiles(fileInput.files); });

    // ── CSV preview ───────────────────────────────────────────────────────────
    function handleFiles(files) {
        if (!files || files.length === 0) return;
        previewBody.innerHTML = '';
        var totalRows = 0;
        var pending = files.length;

        Array.from(files).forEach(function(file) {
            var reader = new FileReader();
            reader.onload = function(e) {
                var rows = parseCSV(e.target.result);
                rows.forEach(function(row) {
                    var tr = document.createElement('tr');
                    var amt = parseFloat(row.amount);
                    var amtClass = isNaN(amt) ? '' : (amt < 0 ? 'upload-amt--out' : 'upload-amt--in');
                    tr.innerHTML =
                        '<td>' + esc(row.date) + '</td>' +
                        '<td class="upload-desc">' + esc(row.description) + '</td>' +
                        '<td class="ds-table__num ' + amtClass + '">' +
                            (isNaN(amt) ? esc(row.rawAmount) : '$' + Math.abs(amt).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})) +
                        '</td>';
                    previewBody.appendChild(tr);
                    totalRows++;
                });
                pending--;
                if (pending === 0) {
                    var label = files.length === 1
                        ? files[0].name + ' — ' + totalRows + ' rows'
                        : files.length + ' files — ' + totalRows + ' rows';
                    fileLabel.textContent = label;
                    previewTitle.textContent = 'Preview (' + totalRows + ' rows)';
                    preview.hidden = false;
                }
            };
            reader.readAsText(file);
        });
    }

    function parseCSV(text) {
        var lines = text.trim().split(/\r?\n/);
        if (lines.length < 2) return [];
        var headers = splitCSVLine(lines[0]);
        var dateIdx   = findCol(headers, ['transaction date', 'date']);
        var descIdx   = findCol(headers, ['transaction description', 'description', 'memo']);
        var amtIdx    = findCol(headers, ['transaction amount', 'amount', 'debit', 'credit']);
        var rows = [];
        for (var i = 1; i < lines.length; i++) {
            if (!lines[i].trim()) continue;
            var cols = splitCSVLine(lines[i]);
            rows.push({
                date: dateIdx >= 0 ? (cols[dateIdx] || '') : '',
                description: descIdx >= 0 ? (cols[descIdx] || '') : '',
                rawAmount: amtIdx >= 0 ? (cols[amtIdx] || '') : '',
                amount: amtIdx >= 0 ? parseFloat(cols[amtIdx]) : NaN,
            });
        }
        return rows;
    }

    function findCol(headers, candidates) {
        for (var i = 0; i < headers.length; i++) {
            var h = headers[i].toLowerCase().trim();
            for (var j = 0; j < candidates.length; j++) {
                if (h === candidates[j]) return i;
            }
        }
        return -1;
    }

    function splitCSVLine(line) {
        var result = [], cur = '', inQuote = false;
        for (var i = 0; i < line.length; i++) {
            var ch = line[i];
            if (ch === '"') { inQuote = !inQuote; }
            else if (ch === ',' && !inQuote) { result.push(cur.trim()); cur = ''; }
            else { cur += ch; }
        }
        result.push(cur.trim());
        return result;
    }

    function esc(str) {
        return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function clearFiles() {
        fileInput.value = '';
        previewBody.innerHTML = '';
        preview.hidden = true;
        fileLabel.textContent = 'CSV files only';
    }
    window.clearFiles = clearFiles;

    // Show uploading status on submit
    form.addEventListener('submit', function() {
        uploadBtn.disabled = true;
        uploadBtn.textContent = 'Uploading…';
        uploadStatus.hidden = false;
    });
}());
