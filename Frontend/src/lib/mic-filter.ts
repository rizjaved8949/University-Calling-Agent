import type { AudioFilter } from "infobip-rtc";

/**
 * Clean up the operator's microphone before it goes down the phone line.
 *
 * Two problems, one graph.
 *
 * Noise: the SDK asks the browser for noise suppression, but a laptop mic still
 * sends the room — fan hum, hiss, keyboard, and the open channel between
 * sentences that a phone line renders as a wash of noise.
 *
 *   high-pass 120 Hz   hum, rumble, desk thumps, mic handling
 *   low-pass  7.5 kHz  hiss lives here, and the phone codec discards the band
 *   compressor         evens out how far you sit from the mic
 *   noise gate         during silence the mic goes to actual silence
 *
 * Echo: the browser cancels what its own speakers play, but it cannot cancel
 * what a phone on loudspeaker sends back. Once the far end re-transmits your
 * voice, every round trip re-amplifies it and you get a ringing tail that
 * restarts each time you speak. Nothing at the far end is ours to configure, so
 * the graph goes half-duplex instead: while the far end is talking, the mic
 * ducks. A loop broken in one direction cannot ring.
 *
 * The gate fades rather than cuts (attack/release below); a hard switch chops
 * the first consonant off every sentence and sounds worse than the noise.
 */

/** Anything quieter than this is treated as room, not speech. */
const GATE_THRESHOLD = 0.012;
/** How fast the gate opens once you speak. Short, or words lose their start. */
const GATE_ATTACK_S = 0.02;
/** How fast it closes again. Long enough to ride over gaps between words. */
const GATE_RELEASE_S = 0.35;
/** Keep the gate open this long after the last sound above the threshold. */
const GATE_HOLD_MS = 250;

/** Far-end speech loud enough to be worth ducking against. */
const DUCK_THRESHOLD = 0.015;
/**
 * How far down the mic goes while the far end talks. Not to zero by default —
 * you must still be able to interrupt, and a dead mic sounds like a dropped
 * call. Low enough that what does get through cannot survive another lap.
 */
const DUCK_GAIN = 0.08;
/**
 * Stay ducked this long after they stop. Longer than the line's round trip on
 * purpose: an echo arrives *after* the sound that caused it, so releasing at
 * the moment the far end goes quiet is releasing exactly into the returning lap.
 */
const DUCK_HOLD_MS = 600;

/**
 * @param farEndLevel Current RMS of the audio arriving from the student, or 0
 *   when nothing is playing. Supplied by the call, which owns that stream.
 * @param strict When this returns true the duck becomes a full mute: strict
 *   half-duplex, no interrupting, and no loop of any gain can sustain. The
 *   escape hatch for a room that feeds back no matter what.
 * @param onSpeaking Fired on each transition of "the counselor is talking".
 *   Ducking the mic stops an echo *growing*; it does nothing about the lap
 *   already in flight, which arrives back a moment later and is the voice you
 *   actually hear repeating. Killing that means turning the earpiece down while
 *   you talk, and only the call knows where the earpiece is — so the gate
 *   reports, and the call acts.
 */
export function createMicFilter(
  farEndLevel: () => number = () => 0,
  strict: () => boolean = () => false,
  onSpeaking: (speaking: boolean) => void = () => {},
): AudioFilter {
  let context: AudioContext | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let destination: MediaStreamAudioDestinationNode | null = null;
  let timer: number | null = null;

  return {
    async start(track) {
      // The SDK picked the device; we only tighten what it asked for. Echo
      // cancellation is the one that matters and is worth asserting rather than
      // assuming — some Windows input drivers quietly ignore it. A browser that
      // rejects a constraint is not a reason to fail the call, so this never
      // throws.
      try {
        await track.applyConstraints({
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        });
      } catch {
        // Ignored on purpose: unsupported constraints are not a call failure.
      }

      context = new AudioContext();
      // Chrome suspends a context created outside a gesture; dialling is a
      // click, but a resume costs nothing when it was never suspended.
      await context.resume().catch(() => undefined);

      source = context.createMediaStreamSource(new MediaStream([track]));

      const highPass = context.createBiquadFilter();
      highPass.type = "highpass";
      highPass.frequency.value = 120;

      const lowPass = context.createBiquadFilter();
      lowPass.type = "lowpass";
      lowPass.frequency.value = 7500;

      const compressor = context.createDynamicsCompressor();
      compressor.threshold.value = -28;
      compressor.knee.value = 12;
      compressor.ratio.value = 4;
      compressor.attack.value = 0.005;
      compressor.release.value = 0.25;

      const gate = context.createGain();
      gate.gain.value = 0;

      // Level is measured *before* the gate — reading it after would mean the
      // gate can never hear the speech that should reopen it.
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      const samples = new Float32Array(analyser.fftSize);

      destination = context.createMediaStreamDestination();

      source.connect(highPass);
      highPass.connect(lowPass);
      lowPass.connect(analyser);
      lowPass.connect(compressor);
      compressor.connect(gate);
      gate.connect(destination);

      let openUntil = 0;
      let duckUntil = 0;
      let announced = false;
      timer = window.setInterval(() => {
        if (!context) return;
        analyser.getFloatTimeDomainData(samples);
        let sum = 0;
        for (const sample of samples) sum += sample * sample;
        const rms = Math.sqrt(sum / samples.length);

        const now = performance.now();
        if (rms > GATE_THRESHOLD) openUntil = now + GATE_HOLD_MS;
        if (farEndLevel() > DUCK_THRESHOLD) duckUntil = now + DUCK_HOLD_MS;

        const open = now < openUntil;
        const ducked = now < duckUntil;
        const target = open ? (ducked ? (strict() ? 0 : DUCK_GAIN) : 1) : 0;

        if (open !== announced) {
          announced = open;
          onSpeaking(open);
        }

        gate.gain.setTargetAtTime(
          target,
          context.currentTime,
          target > gate.gain.value ? GATE_ATTACK_S : GATE_RELEASE_S,
        );
      }, 50);

      return destination.stream.getAudioTracks()[0] as typeof track;
    },

    async stop() {
      if (timer !== null) window.clearInterval(timer);
      timer = null;
      source?.disconnect();
      destination?.disconnect();
      await context?.close().catch(() => undefined);
      source = null;
      destination = null;
      context = null;
    },
  };
}

/**
 * Watch how loud the student is, for the ducking above.
 *
 * The stream must also be attached to an audio element and playing: Chrome
 * gives a silent MediaStreamAudioSourceNode for a remote track nothing is
 * rendering, and a silent probe would mean the mic never ducks.
 */
export function createLevelProbe(stream: MediaStream): { level: () => number; stop: () => void } {
  let context: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let samples = new Float32Array(0);

  try {
    context = new AudioContext();
    void context.resume().catch(() => undefined);
    analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    samples = new Float32Array(analyser.fftSize);
    context.createMediaStreamSource(stream).connect(analyser);
  } catch {
    context = null;
    analyser = null;
  }

  return {
    level: () => {
      if (!analyser) return 0;
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) sum += sample * sample;
      return Math.sqrt(sum / samples.length);
    },
    stop: () => {
      analyser?.disconnect();
      void context?.close().catch(() => undefined);
      analyser = null;
      context = null;
    },
  };
}
