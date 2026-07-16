'use strict';
// Read-only confirmation of the client map-download seam.
// TransferTarget is embedded in ClientRouter at +0x208; these offsets are
// TransferTarget-relative. Counters: received=+0x38, total=+0x80, base=+0xa8,
// writeStream=+0x78. Completion invariant: received == base + total.
const B = Process.getModuleByName('factorio').base;
console.log('[+] factorio base ' + B);
const OFF_requestBlock  = 0x2c252f0;
const OFF_stopDownload  = 0x2bfbff0;
const rd = (p, o) => p.add(o).readU64();

let TT = null, pollId = null;
Interceptor.attach(B.add(OFF_requestBlock), {
  onEnter(a) {
    if (TT) return;
    TT = a[0];                       // TransferTarget* (== ClientRouter+0x208)
    console.log('[+] captured TransferTarget this=' + TT + ' (first requestBlock)');
    pollId = setInterval(() => {
      try {
        const recv = rd(TT, 0x38), total = rd(TT, 0x80), base = rd(TT, 0xa8);
        if (total.toNumber() === 0) return;         // idle between downloads
        const want = base.add(total);
        const pct = (recv.toNumber() * 100 / want.toNumber()).toFixed(1);
        console.log('    progress recv=' + recv + ' / ' + want + '  (' + pct + '%)');
      } catch (e) { console.log('poll err ' + e); }
    }, 1000);
  }
});

Interceptor.attach(B.add(OFF_stopDownload), {
  onEnter(a) {
    this.ret = a[0];                 // hidden return-string buffer
    const self = a[1];               // TransferTarget* this
    const recv = rd(self, 0x38), total = rd(self, 0x80), base = rd(self, 0xa8);
    const want = base.add(total);
    console.log('\n=== [stopDownloadWithMutexHeld] this=' + self + ' ===');
    console.log('  received(+0x38) = ' + recv);
    console.log('  base(+0xa8)     = ' + base);
    console.log('  total(+0x80)    = ' + total);
    console.log('  base+total      = ' + want);
    console.log('  >>> INVARIANT (recv == base+total): ' + (recv.toString() === want.toString()));
    console.log('  writeStream(+0x78) = ' + self.add(0x78).readPointer());
    if (total.toNumber() > 0) {           // the real completion (not init/teardown)
      if (pollId !== null) { clearInterval(pollId); pollId = null; }
      send('EVENT:download-complete');
    }
  }
});
console.log('[+] armed: requestBlock(capture+poll) + stopDownloadWithMutexHeld(confirm)');
