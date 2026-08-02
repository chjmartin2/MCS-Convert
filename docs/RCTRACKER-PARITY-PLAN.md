# RCTracker Ascension — retiring MCS-Player

The plan to bring every MCS-Player capability into RCTracker (and RCPlay),
plus the tracker features MCS-Player never had. Agreed decisions:

- **Target modes lint + snap** (edit freely; unexpressible cells highlighted;
  export quantizes) — never hard-block entry.
- **Retrack replaces the tracker content** with an automatic undo snapshot.
- **Arbitrary BPM stored in the v1 header's reserved bytes** (exact sub-tick
  period in µs; 0 = derive from the MCS tempo byte — full back-compat).
- **Full multi-level undo** (~100 steps, all mutations).

## Phases (commit each)

- [x] **0. Plan + BPM foundation**: `subtick_us` in .RCT (reserved bytes),
      free-BPM plumbing through effects/streams/convert; MCS byte becomes a
      derived snap, engines use exact PIT dividers.
- [x] **1. Transport**: follow mode (playback rolls the pattern view through
      the order list), play-from-cursor, volume slider, per-channel mute/solo,
      multi-level undo/redo.
- [x] **2. Visualizations**: ScopeWindow extracted to viz.py; Scope/VU/Spectrum
      buttons in RCTracker **and** RCPlay; DOS-replica window in RCTracker.
- [x] **3. Export center**: full ExportDialog port for RctSong — target list +
      options, byte/drop readouts, **MCS notation preview** (encode → decode →
      staves + round-trip audio), **Retrack-into-editor**, Exhaustive
      Optimize / Optimize-at-tempo, WAV + text-grid exports.
- [x] **4. Target modes**: per-song mode (Free/MCS/Tandy/1-voice/4-voice/SB)
      with cell linting and tempo snap display ("MCS-mode").
- [x] **5. Import parity**: ImportPreview dialog port (channel stats, solo
      audition, octave shifts, percussion modes, tempo fitting).
- [x] **6. Editor QoL**: block select/copy/paste, insert/delete row,
      transpose; play-through target auditioning (incl. the MCS 1-bit
      speaker model).
- [ ] **7. Retirement**: README/releases lead with RCTracker + RCPlay;
      `play` demoted to MCS format inspector.
