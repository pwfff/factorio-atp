'use strict';
// The client-side map bridge.
//
// Instead of downloading the map over Factorio's request-per-block UDP path,
// we take the copy delivered out-of-band by `atp recv`, inject it straight
// into the game's own write stream, fix the counters, and steer stock
// requestBlock to fetch ONLY the tiny aux block over stock UDP. The client
// then loads + joins bit-identically with no desync -- atp is a drop-in
// replacement for "where the map bytes come from". (The map source is chosen
// by whoever set ATP_SNAP_PATH_FILE: `atp recv` output in normal use, or a
// local file for cold verification.)
//
// Mechanism (from requestBlock disas, 0x2c252f0):
//   requestBlock drains a free-list (retry stack, empty at start) then issues
//   the SEQUENTIAL counter +0xc0 up to blocksNeededFor(base)+blocksNeeded_file.
//   The sequential path does NOT consult the bitmap. So to request only aux:
//     - write the whole map through BufferedFileWriteStream::write (offset 0)
//     - received(+0x38) += total            (byte-count the file bytes we spliced)
//     - nextSequential(+0xc0) = blocksNeeded_file(+0xb0)
//   First requestBlock then issues block index == blocksNeeded_file (the single
//   90-B aux block); the next call sees seq >= totalBlocks and requests nothing.
//   Aux apply adds `base` -> received == base+total -> completion fires.
//
// All offsets TransferTarget-relative (TransferTarget == ClientRouter+0x208).
//
// MAP_SRC is resolved at splice time from work/current-snapshot.path, which
// names THIS join's byte-source. Two producers:
//   - 3a (shared-fs): server-observe.js writes the server's per-join snapshot
//     (mp-save-N.zip); present before the first requestBlock.
//   - 3b (atp): the client-side driver writes the atp-recv'd local file only
//     AFTER `atp recv` completes (~1.8 s), which can land AFTER the first
//     requestBlock -- so we wait (bounded) for it below.
// Splicing a stale snapshot CRC-mismatches on load (server re-serializes per
// join), so if no fresh path appears within the deadline we REFUSE and fall
// through to the stock download rather than corrupt.
// Absolute path injected by the driver (drive.py prepends
// `var ATP_SNAP_PATH_FILE = "…"`). Fallback keeps the hook runnable standalone.
const SNAP_PATH_FILE = (typeof ATP_SNAP_PATH_FILE !== 'undefined' && ATP_SNAP_PATH_FILE)
  ? ATP_SNAP_PATH_FILE
  : '/tmp/atp-current-snapshot.path';
const SRC_WAIT_S = 20;   // max seconds to wait for the byte-source (recv)

function resolveMapSrc() {
  try {
    const t = File.readAllText(SNAP_PATH_FILE).trim();
    if (t) return t;
  } catch (e) {}
  return null;
}

// Poll for the source path up to SRC_WAIT_S. Blocking the socket thread briefly
// is acceptable: the server's sendDataLoop has no per-peer timeout, and we are
// replacing this request path anyway.
function waitForMapSrc() {
  const deadline = Date.now() + SRC_WAIT_S * 1000;
  let waited = false;
  for (;;) {
    const p = resolveMapSrc();
    if (p) { if (waited) console.log('[+] byte-source ready after wait'); return p; }
    if (Date.now() >= deadline) return null;
    if (!waited) { console.log('[+] waiting up to ' + SRC_WAIT_S + 's for byte-source (' + SNAP_PATH_FILE + ')'); waited = true; }
    Thread.sleep(0.25);
  }
}

const B = Process.getModuleByName('factorio').base;
console.log('[+] factorio base ' + B);
const OFF_write        = 0x2c251e0;   // BufferedFileWriteStream::write(this,buf,len)
const OFF_requestBlock = 0x2c252f0;
const OFF_stopDownload = 0x2bfbff0;

// TransferTarget offsets
const O_received = 0x38, O_stream = 0x78, O_total = 0x80,
      O_base = 0xa8, O_blocksFile = 0xb0, O_nextSeq = 0xc0,
      O_inflight = 0x110;

const rd = (p, o) => p.add(o).readU64();

const bfwWrite = new NativeFunction(B.add(OFF_write), 'void',
                                    ['pointer', 'pointer', 'uint64']);
// libc for mmap-ing the local map source into target memory.
// (Frida 17 removed static Module.getExportByName; use getGlobalExportByName.)
const gx = (n) => Module.getGlobalExportByName(n);
const c_open   = new NativeFunction(gx('open'),  'int',    ['pointer', 'int']);
const c_lseek  = new NativeFunction(gx('lseek'), 'int64',  ['int', 'int64', 'int']);
const c_mmap   = new NativeFunction(gx('mmap'),  'pointer',['pointer', 'ulong', 'int', 'int', 'int', 'long']);
const c_close  = new NativeFunction(gx('close'), 'int',    ['int']);

function mmapSource(mapSrc) {
  const path = Memory.allocUtf8String(mapSrc);
  const fd = c_open(path, 0 /*O_RDONLY*/);
  if (fd < 0) throw new Error('open failed ' + fd + ' ' + mapSrc);
  const size = c_lseek(fd, int64(0), 2 /*SEEK_END*/);
  const addr = c_mmap(NULL, uint64(size.toString()), 1 /*PROT_READ*/, 2 /*MAP_PRIVATE*/, fd, 0);
  c_close(fd);
  if (addr.isNull() || addr.equals(ptr('-1')))
    throw new Error('mmap failed ' + addr);
  return { addr, size: size };
}

let spliced = false, TT = null;
Interceptor.attach(B.add(OFF_requestBlock), {
  onEnter(a) {
    if (spliced) return;
    TT = a[0];
    const total  = rd(TT, O_total);
    const base   = rd(TT, O_base);
    const stream = TT.add(O_stream).readPointer();
    const bnFile = rd(TT, O_blocksFile);
    const recv0  = rd(TT, O_received);
    const seq0   = rd(TT, O_nextSeq);
    console.log('[+] first requestBlock: TT=' + TT +
      ' total=' + total + ' base=' + base + ' blocksFile=' + bnFile +
      ' stream=' + stream + ' recv=' + recv0 + ' seq=' + seq0);

    // Guard: refuse to splice if the target isn't primed — better to fail loud
    // (and fall through to stock download) than corrupt state.
    if (total.toNumber() === 0 || stream.isNull()) {
      console.log('[!] not primed at first requestBlock (total/stream) -> NOT splicing');
      return;
    }
    if (seq0.toNumber() !== 0) {
      console.log('[!] nextSequential already advanced (' + seq0 + ') -> NOT splicing');
      return;
    }
    // Source THIS join's exact bytes (3a: server snapshot; 3b: atp-recv'd file).
    // Wait (bounded) for the source; no path in time -> refuse (never fall back
    // to a stale snapshot: CRC bug), let stock download take over.
    const mapSrc = waitForMapSrc();
    if (!mapSrc) {
      console.log('[!] no byte-source in ' + SNAP_PATH_FILE + ' within deadline' +
        ' -> NOT splicing (falling through to stock download)');
      return;
    }

    try {
      const src = mmapSource(mapSrc);
      console.log('[+] mapped source ' + mapSrc + ' size=' + src.size + ' @ ' + src.addr);
      if (src.size.toNumber() < total.toNumber())
        throw new Error('source (' + src.size + ') smaller than total (' + total + ')');

      // 1) lay the whole map through the game's own buffered write stream at
      //    offset 0. Presetting stream+0x38 (write position) = 0 is belt+braces;
      //    a fresh startDownload stream is already at 0.
      stream.add(0x38).writeU64(0);
      const t0 = Date.now();
      bfwWrite(stream, src.addr, total);
      console.log('[+] spliced ' + total + ' bytes through BufferedFileWriteStream::write in ' +
        (Date.now() - t0) + 'ms');

      // 2) byte-count the file bytes we injected (aux apply will add `base`).
      TT.add(O_received).writeU64(total);
      // 3) steer sequential requests to start at the aux block.
      TT.add(O_nextSeq).writeU64(bnFile);
      spliced = true;
      console.log('[+] counters set: received=' + rd(TT, O_received) +
        ' nextSeq=' + rd(TT, O_nextSeq) + ' (== blocksFile) -> stock will request ONLY aux');
      send('EVENT:spliced');
    } catch (e) {
      console.log('[!] splice failed: ' + e + '\n' + (e.stack || ''));
    }
  },
  onLeave() {
    if (spliced && TT) {
      // requestBlock: `mov 0x110(rbx),r14; incq 0x10(r14)` -> count is at
      // *(*(TT+0x110) + 0x10). (Proof of "only aux requested" is really the
      // server-side "Finished download (90 B)" line; this is a cross-check.)
      const n = TT.add(O_inflight).readPointer().add(0x10).readU64();
      console.log('    [after requestBlock] inflight requests = ' + n + ' (expect 1: the aux block)');
    }
  }
});

Interceptor.attach(B.add(OFF_stopDownload), {
  onEnter(a) {
    const self = a[1];
    const recv = rd(self, O_received), total = rd(self, O_total), base = rd(self, O_base);
    const want = base.add(total);
    if (total.toNumber() === 0) return;   // init/teardown, not the real completion
    console.log('\n=== [stopDownloadWithMutexHeld] this=' + self + ' ===');
    console.log('  received(+0x38) = ' + recv);
    console.log('  base(+0xa8)     = ' + base + '   (aux bytes, fetched over stock UDP)');
    console.log('  total(+0x80)    = ' + total + '   (map bytes, spliced locally)');
    console.log('  >>> INVARIANT (recv == base+total): ' + (recv.toString() === want.toString()));
    send('EVENT:download-complete');
  }
});
console.log('[+] armed (splice): requestBlock(cold-splice) + stopDownloadWithMutexHeld(confirm)');
