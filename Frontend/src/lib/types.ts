export type Direction = "INBOUND" | "OUTBOUND";

/** What the server knows about a call's audio. See `Call.recording_state`. */
export type RecordingState = "RECORDING" | "PENDING" | "READY" | "NONE";

export type TranscriptEntry = {
  id?: string;
  role?: string;
  speaker?: string;
  text?: string;
  content?: string;
  timestamp?: string;
  ts?: string;
};

export type Call = {
  id: string;
  phone_number?: string;
  from_number?: string;
  to_number?: string;
  direction?: Direction;
  status?: string;
  state?: string;
  started_at?: string;
  /** When the call was actually picked up. Absent while it is still ringing. */
  answered_at?: string;
  created_at?: string;
  ended_at?: string;
  duration?: number;
  duration_seconds?: number;
  outcome?: string;
  summary?: string;
  recording_url?: string;
  recording_ready?: boolean;
  /**
   * What the server knows about this call's audio, so the player can say
   * "never recorded" instead of offering a retry that can never succeed.
   * RECORDING while the call is up, PENDING once it ends and the provider is
   * still composing, READY when it can be played, NONE when there is none.
   */
  recording_state?: RecordingState;
  transcript?: TranscriptEntry[];
  /** Absent or "AGENT" when Ayesha handled it; MANUAL_* when a person did. */
  mode?: string;
  /** Manual phone-bridge calls only: the counselor's own number. */
  operator_number?: string;
};

export type AppConfig = {
  phone_number?: string;
  knowledge_base_ready?: boolean;
  telephony_ready?: boolean;
  manual_phone_ready?: boolean;
  manual_browser_ready?: boolean;
  operator_phone_number?: string;
  /**
   * Whether the server can build the master call-record workbook. False on a
   * deployment whose image predates openpyxl, and the reason the Download Excel
   * button is hidden rather than offered and then failing.
   */
  excel_ready?: boolean;
  excel_filename?: string;
  summary_enabled?: boolean;
  /** The reference-number template is approved and Ayesha may offer it. */
  whatsapp_ready?: boolean;
  /**
   * The second template is approved, so the information itself can be sent in
   * writing - not just a reference number. False means the agent is not even
   * offered the tool, so she never promises a message that cannot be sent.
   */
  whatsapp_details_ready?: boolean;
};

export type RtcToken = {
  token?: string;
  identity?: string;
  expires_in?: number;
  from?: string;
};

export type CallStats = {
  total_calls?: number;
  inbound_calls?: number;
  outbound_calls?: number;
  inbound?: number;
  outbound?: number;
  total_duration?: number;
  talk_time?: number;
  active_calls?: number;
  active?: number;
  recordings?: number;
  [key: string]: unknown;
};

/** The backend answers with `{answer, sources, session_id}`. */
export type ChatSource = {
  section?: string;
  title?: string;
  page?: number;
  score?: number;
  text?: string;
};

export type ChatReply = {
  answer?: string;
  reply?: string;
  message?: string;
  response?: string;
  sources?: Array<string | ChatSource>;
  session_id?: string;
};

export type RealtimeEventType =
  | "snapshot"
  | "call.created"
  | "call.updated"
  | "transcript"
  | "rag.query"
  | "agent.interrupted"
  | "recording.ready"
  | "agent.accepted"
  | "agent.error"
  | "call.error";

export type RealtimeEvent = {
  type: RealtimeEventType | string;
  call_id?: string;
  data?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  timestamp?: string;
  [key: string]: unknown;
};
