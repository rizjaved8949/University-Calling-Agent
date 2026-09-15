/**
 * Recording a Talk-tab call in the browser, because the provider will not.
 *
 * Calls dialled from here run under Infobip's built-in WEBRTC calls
 * configuration. That configuration is not ours, has recording switched off,
 * and the API that used to configure it is gone — every attempt to start a
 * recording on one is refused. The browser is the one place that holds both
 * halves of the conversation anyway: the microphone it is sending, and the
 * stream it is receiving. So it records them itself and posts the file to our
 * own storage.
 *
 * Both sides are mixed into a single track rather than recorded separately.
 * Two files would need composing afterwards, and the whole point of doing this
 * here is to stop depending on anyone else's composition step.
 *
 * What must end up in the file: the operator's voice once, the student's voice
 * once, and nothing else. The two ways that goes wrong are the speaker bleeding
 * back into the microphone (handled by echo cancellation below) and the two
 * sources clipping when summed (handled by the gain staging). Ringing is not in
 * the file either — recording starts at pickup, not at dial.
 */

/** What MediaRecorder will actually accept, best container first. */
const CANDIDATE_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  return CANDIDATE_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
}

export type CallRecorder = {
  /** Stop recording and hand back the audio, or null if there is none. */
  stop: () => Promise<Blob | null>;
};

/**
 * Start recording a call already in progress.
 *
 * Never throws: a call that cannot be recorded must still be a call. Returns
 * null when the browser has no MediaRecorder or the microphone cannot be
 * opened, and the caller carries on regardless.
 */
export async function startCallRecorder(
  remote: MediaStream,
  onProblem: (reason: string) => void = () => undefined,
): Promise<CallRecorder | null> {
  const mimeType = pickMimeType();
  if (!mimeType) {
    onProblem("this browser has no MediaRecorder that can produce audio");
    return null;
  }

  let context: AudioContext | null = null;
  let mic: MediaStream | null = null;
  try {
    // Echo cancellation is not optional here, and this is the subtle part.
    //
    // The student's voice arrives on `remote` AND comes out of the operator's
    // speakers, where an unguarded microphone picks it up again a fraction of a
    // second later. Mixing those two together puts the student in the recording
    // twice, slightly offset — which is heard as an echo, or as every sentence
    // being repeated. Cancellation removes the speaker signal from the captured
    // microphone, so each voice lands exactly once.
    //
    // Headphones solve it physically; most people are not wearing any.
    mic = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const audio = new AudioContext();
    context = audio;
    await audio.resume().catch(() => undefined);
    const mixed = audio.createMediaStreamDestination();

    // Both sides through their own gain, a little under unity. Two voices
    // summed at full scale clip when people talk over each other, and clipping
    // is heard as a crackle exactly at the moments of the call that matter.
    const level = (source: MediaStream) => {
      const gain = audio.createGain();
      gain.gain.value = 0.8;
      audio.createMediaStreamSource(source).connect(gain);
      gain.connect(mixed);
    };
    level(mic);
    level(remote);

    const recorder = new MediaRecorder(mixed.stream, { mimeType });
    const chunks: BlobPart[] = [];
    recorder.ondataavailable = (event) => {
      if (event.data.size) chunks.push(event.data);
    };
    // A timeslice means data arrives as the call runs, so a browser closed
    // mid-call still leaves us whatever had already been flushed, rather than
    // one final chunk that never arrives.
    recorder.start(5_000);

    const cleanup = () => {
      mic?.getTracks().forEach((track) => track.stop());
      void context?.close().catch(() => undefined);
      mic = null;
      context = null;
    };

    return {
      stop: () =>
        new Promise<Blob | null>((resolve) => {
          if (recorder.state === "inactive") {
            cleanup();
            resolve(chunks.length ? new Blob(chunks, { type: mimeType }) : null);
            return;
          }
          recorder.onstop = () => {
            cleanup();
            resolve(chunks.length ? new Blob(chunks, { type: mimeType }) : null);
          };
          recorder.stop();
        }),
    };
  } catch (err) {
    mic?.getTracks().forEach((track) => track.stop());
    void context?.close().catch(() => undefined);
    // The name matters more than the message: NotReadableError means the device
    // is already held, NotAllowedError means permission, NotFoundError means no
    // microphone. Each one is a different fix, and without it we are guessing.
    const detail =
      err instanceof DOMException ? `${err.name}: ${err.message}` : String(err).slice(0, 200);
    onProblem(`could not start recording — ${detail}`);
    return null;
  }
}
