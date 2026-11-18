// Downmix to mono, resample to 16 kHz and post 50 ms chunks of 16-bit PCM.
const TARGET_RATE = 16000;
const CHUNK = 800; // 50 ms at 16 kHz: AssemblyAI's recommended frame size

class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.pos = 0;
    this.prev = 0;
    this.out = new Int16Array(CHUNK);
    this.n = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels || channels.length === 0) return true;
    const len = channels[0].length;
    for (let i = 0; i < len; i++) {
      let s = 0;
      for (let c = 0; c < channels.length; c++) s += channels[c][i];
      s /= channels.length;
      // Linear interpolation between the previous and current sample.
      while (this.pos <= 1) {
        const v = this.prev + (s - this.prev) * this.pos;
        const clamped = Math.max(-1, Math.min(1, v));
        this.out[this.n++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
        if (this.n === CHUNK) {
          this.port.postMessage(this.out.buffer, [this.out.buffer]);
          this.out = new Int16Array(CHUNK);
          this.n = 0;
        }
        this.pos += this.step;
      }
      this.pos -= 1;
      this.prev = s;
    }
    return true;
  }
}

registerProcessor("pcm16-downsampler", Pcm16Downsampler);
