'use strict';
// Server-side snapshot detector (deterministic — replaces /proc fd polling).
//
// On each join, ServerMultiplayerManager::applySynchronizerActions (0x2d25f50)
// formats a per-join save name `mp-save-%u` (counter @ this+0x238, incremented
// every join -> the filename CHANGES per join, so a fixed-name lookup is wrong),
// serializes the current game to <write-data>/temp/mp-save-N.zip, then
// TransferSource opens THAT file O_RDONLY and serves it block-by-block.
//
// We hook openat/open and classify by access mode:
//   - write-create open (O_WRONLY/O_CREAT)  -> snapshot is being written
//   - read open        (O_RDONLY)           -> snapshot is written & TransferSource
//     is about to serve it == "ready". We emit EVENT:snapshot-ready path=<p>;
//     server-drive.py records it to work/current-snapshot.path for splice.js.
//
// This same "ready + path" signal is the server-bridge trigger: the server
// atp-sends the snapshot instead of / alongside the stock serve.
const gx = (n) => Module.getGlobalExportByName(n);
const O_ACCMODE = 3, O_RDONLY = 0, O_CREAT = 0x40;
const isSnapshot = (p) => p && /mp-save-\d+\.zip/.test(p);
// Only the per-join snapshot matters; the `currently-playing/**` tree (locale,
// etc.) is opened constantly and would flood the log, so scope tightly.
const saveish    = (p) => isSnapshot(p) || (p && /mp-download\.zip/.test(p));

let readyEmitted = null;   // path we've already announced (dedupe repeat opens)

function onOpen(path, flags, fd) {
  if (!saveish(path)) return;
  const acc = flags & O_ACCMODE;
  const creating = (acc !== O_RDONLY) || (flags & O_CREAT);
  if (isSnapshot(path)) {
    if (creating) {
      console.log('[snapshot:write-create] fd=' + fd + ' flags=0x' + flags.toString(16) + '  ' + path);
    } else if (readyEmitted !== path) {
      // O_RDONLY open of a fully-written snapshot -> ready to serve.
      readyEmitted = path;
      console.log('[snapshot:READY] fd=' + fd + '  ' + path + '  (written; TransferSource serving)');
      send('EVENT:snapshot-ready path=' + path);
    }
  } else {
    console.log('[open] fd=' + fd + ' flags=0x' + flags.toString(16) + '  ' + path);
  }
}

Interceptor.attach(gx('openat'), {
  onEnter(a) { this.path = a[1].readCString(); this.flags = a[2].toInt32(); },
  onLeave(r) { onOpen(this.path, this.flags, r.toInt32()); }
});
try {
  Interceptor.attach(gx('open'), {
    onEnter(a) { this.path = a[0].readCString(); this.flags = a[1].toInt32(); },
    onLeave(r) { onOpen(this.path, this.flags, r.toInt32()); }
  });
} catch (e) {}

console.log('[+] armed (server-observe): snapshot detector (openat/open classify)');
