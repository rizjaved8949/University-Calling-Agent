import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type Lang = "en" | "ur";

const dict = {
  appName: { en: "Admissions Voice Agent", ur: "ایڈمشنز وائس ایجنٹ" },
  university: { en: "University", ur: "یونیورسٹی" },
  home: { en: "Home", ur: "ہوم" },
  call: { en: "Call", ur: "کال" },
  manual: { en: "Talk", ur: "خود بات کریں" },
  history: { en: "History", ur: "ریکارڈ" },
  ask: { en: "Ask", ur: "پوچھیں" },
  live: { en: "Live", ur: "لائیو" },
  reconnecting: { en: "Reconnecting…", ur: "دوبارہ رابطہ…" },
  offline: { en: "Offline", ur: "آف لائن" },
  agentNumber: { en: "Agent number", ur: "ایجنٹ نمبر" },
  copied: { en: "Copied to clipboard", ur: "کاپی ہو گیا" },
  language: { en: "اردو", ur: "English" },
  theme: { en: "Theme", ur: "تھیم" },
  totalCalls: { en: "Total calls", ur: "کل کالز" },
  inbound: { en: "Inbound", ur: "موصول" },
  outbound: { en: "Outbound", ur: "بھیجی گئی" },
  talkTime: { en: "Talk time", ur: "گفتگو کا وقت" },
  activeNow: { en: "Active now", ur: "ابھی جاری" },
  recordings: { en: "Recordings", ur: "ریکارڈنگز" },
  recentCalls: { en: "Recent calls", ur: "حالیہ کالز" },
  liveActivity: { en: "Live activity", ur: "لائیو سرگرمی" },
  agentHealth: { en: "Agent health", ur: "ایجنٹ کی حالت" },
  knowledgeBase: { en: "Knowledge base", ur: "معلوماتی ذخیرہ" },
  phoneLine: { en: "Phone line", ur: "فون لائن" },
  ready: { en: "Ready", ur: "تیار" },
  connected: { en: "Connected", ur: "منسلک" },
  notReady: { en: "Not ready", ur: "تیار نہیں" },
  placeCall: { en: "Place a call", ur: "کال کریں" },
  callStudent: { en: "Call a student", ur: "طالبعلم کو کال کریں" },
  phoneNumber: { en: "Phone number", ur: "فون نمبر" },
  clear: { en: "Clear", ur: "صاف کریں" },
  paste: { en: "Paste", ur: "پیسٹ" },
  recentNumbers: { en: "Recent numbers", ur: "حالیہ نمبر" },
  confirmCall: { en: "Place this call?", ur: "کیا یہ کال کریں؟" },
  realCallNote: {
    en: "This will place a real phone call. Ayesha will speak with the student in Urdu or English.",
    ur: "یہ حقیقی کال کرے گی۔ عائشہ طالبعلم سے اردو یا انگریزی میں بات کرے گی۔",
  },
  cancel: { en: "Cancel", ur: "منسوخ" },
  endCall: { en: "End call", ur: "کال ختم کریں" },
  confirmEnd: { en: "End this call?", ur: "کال ختم کریں؟" },
  confirmEndNote: {
    en: "The student will be disconnected immediately.",
    ur: "طالبعلم کا رابطہ فوراً منقطع ہو جائے گا۔",
  },
  transcript: { en: "Live transcript", ur: "لائیو ٹرانسکرپٹ" },
  jumpToLatest: { en: "Jump to latest", ur: "تازہ ترین پر جائیں" },
  knowledgeChecked: { en: "Knowledge checked", ur: "معلومات دیکھی گئیں" },
  interrupted: { en: "Caller interrupted", ur: "کالر نے بات کاٹی" },
  callAgain: { en: "Call again", ur: "دوبارہ کال کریں" },
  search: { en: "Search by number", ur: "نمبر سے تلاش کریں" },
  allDirections: { en: "All directions", ur: "تمام" },
  exportCsv: { en: "Export CSV", ur: "CSV ایکسپورٹ" },
  downloadExcel: { en: "Download Excel", ur: "ایکسل ڈاؤن لوڈ" },
  clearFilters: { en: "Clear filters", ur: "فلٹر صاف کریں" },
  noResults: { en: "No calls match these filters", ur: "ان فلٹرز سے کوئی کال نہیں ملی" },
  noCallsYet: { en: "No calls yet", ur: "ابھی کوئی کال نہیں" },
  noCallsBody: {
    en: "When Ayesha speaks with a student, the call appears here with its recording and transcript.",
    ur: "جب عائشہ کسی طالبعلم سے بات کرے گی، کال یہاں ریکارڈنگ اور ٹرانسکرپٹ کے ساتھ نظر آئے گی۔",
  },
  askAyesha: { en: "Ask Ayesha", ur: "عائشہ سے پوچھیں" },
  askBanner: {
    en: "This is the same knowledge the agent uses on calls — a safe place to test.",
    ur: "یہ وہی معلومات ہیں جو ایجنٹ کالز میں استعمال کرتی ہے — جانچنے کی محفوظ جگہ۔",
  },
  askPlaceholder: {
    en: "Ask about fees, admissions, hostel…",
    ur: "فیس، ایڈمشن، ہاسٹل کے بارے میں پوچھیں…",
  },
  send: { en: "Send", ur: "بھیجیں" },
  copy: { en: "Copy", ur: "کاپی" },
  sources: { en: "Sources", ur: "حوالہ جات" },
  duration: { en: "Duration", ur: "دورانیہ" },
  outcome: { en: "Outcome", ur: "نتیجہ" },
  when: { en: "When", ur: "کب" },
  number: { en: "Number", ur: "نمبر" },
  direction: { en: "Direction", ur: "قسم" },
  recording: { en: "Recording", ur: "ریکارڈنگ" },
  download: { en: "Download", ur: "ڈاؤن لوڈ" },
  play: { en: "Play", ur: "چلائیں" },
  recordingLoading: { en: "Loading the recording…", ur: "ریکارڈنگ لوڈ ہو رہی ہے…" },
  recordingPending: {
    en: "The recording is still being prepared. This usually takes a minute after the call ends.",
    ur: "ریکارڈنگ ابھی تیار ہو رہی ہے۔ کال ختم ہونے کے بعد عموماً ایک منٹ لگتا ہے۔",
  },
  recordingFailedLoad: {
    en: "We couldn't load the recording.",
    ur: "ریکارڈنگ لوڈ نہیں ہو سکی۔",
  },
  recordingNone: {
    en: "This call was not recorded.",
    ur: "اس کال کی ریکارڈنگ نہیں ہوئی۔",
  },
  copyTranscript: { en: "Copy transcript", ur: "ٹرانسکرپٹ کاپی کریں" },
  noTranscript: {
    en: "No transcript was captured for this call.",
    ur: "اس کال کا ٹرانسکرپٹ دستیاب نہیں۔",
  },
  serverDown: { en: "Can't reach the server, retrying…", ur: "سرور سے رابطہ نہیں، دوبارہ کوشش…" },
  serverDownBody: {
    en: "The console will fill in automatically as soon as the connection returns.",
    ur: "رابطہ بحال ہوتے ہی معلومات خودبخود آ جائیں گی۔",
  },
  retry: { en: "Try again", ur: "دوبارہ کوشش" },
  callInProgress: { en: "A call is in progress", ur: "ایک کال جاری ہے" },
  goToCall: { en: "Open live call", ur: "لائیو کال کھولیں" },
  welcome: { en: "Good day", ur: "خوش آمدید" },
  welcomeBody: {
    en: "Ayesha is answering admissions questions in Urdu and English, around the clock.",
    ur: "عائشہ ہر وقت اردو اور انگریزی میں ایڈمشن سے متعلق سوالات کے جواب دے رہی ہے۔",
  },
  waitingEvents: { en: "Waiting for activity", ur: "سرگرمی کا انتظار" },
  waitingEventsBody: {
    en: "Live call events will stream in here as soon as something happens.",
    ur: "کوئی سرگرمی ہوتے ہی لائیو ایونٹس یہاں دکھائی دیں گے۔",
  },

  // Call status, in the words someone watching the dashboard would use. The
  // backend's own values are a state machine (DIALING, CONNECTING_AGENT,
  // BRIDGED) and were being printed raw; these are what they MEAN. Mapped in
  // statusKey() in normalize.ts, which is the only place that should know the
  // backend's spelling.
  statusDialing: { en: "Dialling", ur: "ملایا جا رہا ہے" },
  statusRinging: { en: "Ringing", ur: "گھنٹی بج رہی ہے" },
  statusAnswered: { en: "Picked up", ur: "کال اٹھا لی" },
  statusConnecting: { en: "Connecting Ayesha", ur: "عائشہ کو ملایا جا رہا ہے" },
  statusBridged: { en: "On call", ur: "بات جاری ہے" },
  statusEnding: { en: "Hanging up", ur: "کال بند ہو رہی ہے" },
  statusEnded: { en: "Ended", ur: "ختم" },
  statusFailed: { en: "Failed", ur: "ناکام" },
  statusNoAnswer: { en: "No answer", ur: "کوئی جواب نہیں" },
  statusBusy: { en: "Busy", ur: "مصروف" },
  statusUnknown: { en: "Unknown", ur: "نامعلوم" },
} as const;

export type TKey = keyof typeof dict;

type Ctx = { lang: Lang; setLang: (l: Lang) => void; t: (k: TKey) => string };
const LangContext = createContext<Ctx | null>(null);

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>("en");

  useEffect(() => {
    const stored = window.localStorage.getItem("lang");
    if (stored === "ur" || stored === "en") setLangState(stored);
  }, []);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    window.localStorage.setItem("lang", l);
  }, []);

  const t = useCallback((k: TKey) => dict[k][lang], [lang]);

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

export function useI18n() {
  const ctx = useContext(LangContext);
  if (!ctx) throw new Error("useI18n must be used inside LanguageProvider");
  return ctx;
}
