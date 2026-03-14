"use client";

import {
  useEffect,
  useRef,
  useState,
  useCallback,
  Fragment,
} from "react";
import { AudioEngine } from "./lib/audioEngine";

// ─── Types ───────────────────────────────────────────────────────────────────

type Phase = "idle" | "loading" | "playing" | "quiz" | "done";

interface Visual {
  type: "video" | "image";
  data: string; // base64
  mimeType: string;
  beatIndex: number;
}

interface QuizQuestion {
  question: string;
  options: string[];
  correct: string; // "A" | "B" | "C" | "D"
  explanation: string;
}

interface QuizState {
  questions: QuizQuestion[];
  currentIdx: number;
  answers: (string | null)[];
  score: number;
  showExplanation: boolean;
  topic: string;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws";

const BEAT_LABELS = [
  "Hook",
  "Foundation",
  "Mechanism",
  "Scale",
  "Counterintuitive",
  "Human Connection",
  "Frontier",
  "Reflection",
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function ChronosCinema() {
  // Core state
  const [phase, setPhase] = useState<Phase>("idle");
  const [topic, setTopic] = useState("");
  const [userName, setUserName] = useState("");
  const [docTitle, setDocTitle] = useState("");
  const [beatDuration, setBeatDuration] = useState(35); // seconds, for Ken Burns timing

  // Cinema state
  const [currentBeat, setCurrentBeat] = useState(-1);
  const [totalBeats, setTotalBeats] = useState(8);
  const [subtitles, setSubtitles] = useState<string[]>([]);
  const [statusLog, setStatusLog] = useState<string[]>([]);
  const [currentVisual, setCurrentVisual] = useState<Visual | null>(null);
  const [bgmActive, setBgmActive] = useState(false);

  // Visual queue: indexed by beat
  const [visualByBeat, setVisualByBeat] = useState<Record<number, Visual>>({});

  // Quiz state
  const [quiz, setQuiz] = useState<QuizState | null>(null);

  // Mic / voice
  const [micEnabled, setMicEnabled] = useState(false);
  const [isListening, setIsListening] = useState(false);

  // Refs
  const wsRef = useRef<WebSocket | null>(null);
  const audioEngineRef = useRef<AudioEngine | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const subtitleScrollRef = useRef<HTMLDivElement>(null);
  const statusLogRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const currentBeatRef = useRef(-1);
  const visualByBeatRef = useRef<Record<number, Visual>>({});

  // Keep ref in sync
  useEffect(() => {
    currentBeatRef.current = currentBeat;
  }, [currentBeat]);

  useEffect(() => {
    visualByBeatRef.current = visualByBeat;
  }, [visualByBeat]);

  // ── Subtitle auto-scroll ──
  useEffect(() => {
    if (subtitleScrollRef.current) {
      subtitleScrollRef.current.scrollTop =
        subtitleScrollRef.current.scrollHeight;
    }
  }, [subtitles]);

  // ── Status log auto-scroll ──
  useEffect(() => {
    if (statusLogRef.current) {
      statusLogRef.current.scrollTop = statusLogRef.current.scrollHeight;
    }
  }, [statusLog]);

  const addStatus = useCallback((msg: string) => {
    setStatusLog((prev) => {
      const next = [...prev, msg];
      return next.slice(-50); // Keep last 50 messages
    });
  }, []);

  const addSubtitle = useCallback((text: string) => {
    if (!text.trim()) return;
    setSubtitles((prev) => {
      const next = [...prev, text.trim()];
      return next.slice(-6); // Keep last 6 subtitle lines
    });
  }, []);

  // ── Audio engine init (first interaction) ──
  const ensureAudio = useCallback(async () => {
    if (!audioEngineRef.current) {
      audioEngineRef.current = new AudioEngine();
    }
    audioEngineRef.current.init();
    await audioEngineRef.current.resume();
  }, []);

  // ── Apply a received visual ──
  const applyVisual = useCallback((visual: Visual) => {
    // Always store by beat index
    setVisualByBeat((prev) => ({ ...prev, [visual.beatIndex]: visual }));

    setCurrentVisual((prev) => {
      // No visual yet → show whatever arrives first
      if (!prev) return visual;
      // Video always upgrades an image for the same beat (silent upgrade)
      if (visual.type === "video" && visual.beatIndex === prev.beatIndex) return visual;
      // Show if it's for the current or a future beat
      if (visual.beatIndex >= currentBeatRef.current) return visual;
      return prev;
    });
  }, []);

  // ── Handle WebSocket messages ──
  // Refs for quiz/story race condition
  const storyCompleteRef = useRef(false);
  const quizReadyRef = useRef(false);

  const handleMessage = useCallback(
    async (data: string) => {
      let msg: Record<string, unknown>;
      try {
        msg = JSON.parse(data);
      } catch {
        return;
      }

      const type = msg.type as string;

      switch (type) {
        case "status": {
          addStatus(msg.content as string);
          break;
        }

        case "topic_received": {
          const t = (msg.title as string) || (msg.content as string);
          setDocTitle(t);
          setPhase("playing");
          break;
        }

        case "beat_start": {
          const beatIdx = msg.beat_index as number;
          const total = msg.total_beats as number;
          const dur = (msg.target_duration_seconds as number) || 35;
          setCurrentBeat(beatIdx);
          setTotalBeats(total);
          setBeatDuration(dur);
          setSubtitles([]);
          // Image for this beat should already be cached (backend waited for it)
          const cached = visualByBeatRef.current[beatIdx];
          if (cached) setCurrentVisual(cached);
          audioEngineRef.current?.duckBgm();
          break;
        }

        case "beat_end": {
          audioEngineRef.current?.restoreBgm();
          break;
        }

        case "audio_chunk": {
          await ensureAudio();
          if (msg.data) {
            audioEngineRef.current?.enqueueNarrationChunk(msg.data as string);
          }
          break;
        }

        case "narration": {
          addSubtitle(msg.content as string);
          break;
        }

        case "video": {
          const beatIdx =
            typeof msg.beat_index === "number"
              ? (msg.beat_index as number)
              : currentBeatRef.current;
          applyVisual({
            type: "video",
            data: msg.data as string,
            mimeType: (msg.mime_type as string) || "video/mp4",
            beatIndex: beatIdx,
          });
          break;
        }

        case "image": {
          const beatIdx =
            typeof msg.beat_index === "number"
              ? (msg.beat_index as number)
              : currentBeatRef.current;
          applyVisual({
            type: "image",
            data: msg.data as string,
            mimeType: (msg.mime_type as string) || "image/png",
            beatIndex: beatIdx,
          });
          break;
        }

        case "bgm_audio": {
          await ensureAudio();
          if (msg.data) {
            await audioEngineRef.current?.playBgm(msg.data as string);
            setBgmActive(true);
          }
          break;
        }

        case "bgm_fallback": {
          setBgmActive(false);
          break;
        }

        case "quiz_data": {
          const questions = msg.questions as QuizQuestion[];
          if (questions && questions.length > 0) {
            setQuiz({
              questions,
              currentIdx: 0,
              answers: new Array(questions.length).fill(null),
              score: 0,
              showExplanation: false,
              topic: (msg.topic as string) || topic,
            });
            quizReadyRef.current = true;
            if (storyCompleteRef.current) setPhase("quiz");
          }
          break;
        }

        case "story_complete": {
          audioEngineRef.current?.restoreBgm();
          storyCompleteRef.current = true;
          if (quizReadyRef.current) setPhase("quiz");
          break;
        }

        case "interrupted": {
          addStatus(`[${msg.source as string}] Interruption detected`);
          break;
        }

        case "error": {
          addStatus(`ERROR: ${msg.content as string}`);
          break;
        }
      }
    },
    [addStatus, addSubtitle, applyVisual, ensureAudio, topic]
  );

  // ── Connect WebSocket ──
  const connect = useCallback(async () => {
    if (wsRef.current) {
      wsRef.current.close();
    }
    await ensureAudio();

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => addStatus("Connected to Chronos Cinema backend");
    ws.onmessage = (event) => handleMessage(event.data as string);
    ws.onclose = () => addStatus("Disconnected from backend");
    ws.onerror = () =>
      addStatus("WebSocket error — check backend is running on port 8000");
  }, [addStatus, ensureAudio, handleMessage]);

  // ── Start documentary ──
  const startDocumentary = useCallback(async () => {
    if (!topic.trim()) return;

    await connect();

    setPhase("loading");
    setCurrentBeat(-1);
    setSubtitles([]);
    setStatusLog([]);
    setCurrentVisual(null);
    setVisualByBeat({});
    setBgmActive(false);
    setQuiz(null);
    setDocTitle(topic);
    storyCompleteRef.current = false;
    quizReadyRef.current = false;

    // Wait a moment for WebSocket to connect
    await new Promise((resolve) => setTimeout(resolve, 400));

    wsRef.current?.send(
      JSON.stringify({
        type: "start",
        topic: topic.trim(),
        name: userName.trim(),
      })
    );
  }, [topic, userName, connect]);

  // ── Mic recording ──
  const toggleMic = useCallback(async () => {
    if (micEnabled) {
      // Stop mic
      mediaRecorderRef.current?.stop();
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
      mediaRecorderRef.current = null;
      mediaStreamRef.current = null;
      setMicEnabled(false);
      setIsListening(false);
      wsRef.current?.send(JSON.stringify({ type: "user_audio_end" }));
    } else {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            sampleRate: 16000,
            echoCancellation: true,
            noiseSuppression: true,
          },
        });
        mediaStreamRef.current = stream;

        const recorder = new MediaRecorder(stream, {
          mimeType: "audio/webm;codecs=opus",
        });

        recorder.ondataavailable = async (e) => {
          if (e.data.size === 0 || !wsRef.current) return;
          // Convert to base64 and send
          const reader = new FileReader();
          reader.onloadend = () => {
            const base64 = (reader.result as string).split(",")[1];
            wsRef.current?.send(
              JSON.stringify({ type: "user_audio", data: base64 })
            );
          };
          reader.readAsDataURL(e.data);
        };

        recorder.start(250); // 250ms chunks
        mediaRecorderRef.current = recorder;
        setMicEnabled(true);
        setIsListening(true);
      } catch (err) {
        addStatus(`Mic error: ${err}`);
      }
    }
  }, [micEnabled, addStatus]);

  // ── Send chat message ──
  const sendChat = useCallback(
    (text: string) => {
      if (!text.trim() || !wsRef.current) return;
      wsRef.current.send(
        JSON.stringify({ type: "user_chat", content: text.trim() })
      );
    },
    []
  );

  // ── Update video element when visual changes ──
  useEffect(() => {
    if (!currentVisual || currentVisual.type !== "video") return;
    if (!videoRef.current) return;

    const blob = base64ToBlob(currentVisual.data, currentVisual.mimeType);
    const url = URL.createObjectURL(blob);
    videoRef.current.src = url;
    videoRef.current.play().catch(() => {});

    return () => {
      URL.revokeObjectURL(url);
    };
  }, [currentVisual]);

  // ── Cleanup on unmount ──
  useEffect(() => {
    return () => {
      wsRef.current?.close();
      audioEngineRef.current?.destroy();
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  // ─── Quiz handlers ────────────────────────────────────────────────────────

  const answerQuiz = useCallback(
    (optionLetter: string) => {
      if (!quiz || quiz.showExplanation) return;
      const q = quiz.questions[quiz.currentIdx];
      const isCorrect = optionLetter === q.correct;
      const newAnswers = [...quiz.answers];
      newAnswers[quiz.currentIdx] = optionLetter;
      setQuiz((prev) =>
        prev
          ? {
              ...prev,
              answers: newAnswers,
              score: isCorrect ? prev.score + 1 : prev.score,
              showExplanation: true,
            }
          : prev
      );
    },
    [quiz]
  );

  const nextQuizQuestion = useCallback(() => {
    if (!quiz) return;
    if (quiz.currentIdx + 1 >= quiz.questions.length) {
      setPhase("done");
    } else {
      setQuiz((prev) =>
        prev
          ? { ...prev, currentIdx: prev.currentIdx + 1, showExplanation: false }
          : prev
      );
    }
  }, [quiz]);

  // ─── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-cinema-black flex flex-col">
      {/* Header */}
      <header className="fixed top-0 left-0 right-0 z-50 flex items-center justify-between px-6 py-3 border-b border-cinema-border bg-cinema-black/95 backdrop-blur-sm">
        <div className="flex items-center gap-3">
          <span className="text-cinema-gold font-bold tracking-widest text-sm uppercase">
            ◈ CHRONOS CINEMA
          </span>
          {docTitle && phase !== "idle" && (
            <span className="text-cinema-muted text-xs truncate max-w-xs">
              / {docTitle}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {bgmActive && phase === "playing" && (
            <div className="flex items-end gap-0.5 h-4">
              {[0, 1, 2, 3, 4].map((i) => (
                <div
                  key={i}
                  className="wave-bar w-1 bg-cinema-gold rounded-full"
                  style={{ height: "8px" }}
                />
              ))}
            </div>
          )}
          {phase === "playing" && (
            <span className="text-xs text-cinema-muted">
              Beat {currentBeat + 1}/{totalBeats}
            </span>
          )}
        </div>
      </header>

      {/* ── IDLE: Start Screen ── */}
      {phase === "idle" && (
        <div className="flex-1 flex items-center justify-center p-6 pt-20">
          <div className="w-full max-w-xl animate-fade-in">
            <div className="text-center mb-12">
              <h1 className="text-5xl font-bold text-gold-gradient mb-4 tracking-tight">
                Chronos Cinema
              </h1>
              <p className="text-cinema-muted text-lg leading-relaxed">
                Enter any topic and watch an AI director, narrator, composer,
                and cinematographer collaborate in real time — generating a
                cinematic documentary just for you.
              </p>
            </div>

            <div className="bg-cinema-card border border-cinema-border rounded-2xl p-8 space-y-6">
              <div className="space-y-2">
                <label className="text-xs font-semibold text-cinema-gold uppercase tracking-widest">
                  Documentary Topic
                </label>
                <input
                  ref={inputRef}
                  type="text"
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  onKeyDown={(e) =>
                    e.key === "Enter" && startDocumentary()
                  }
                  placeholder="Black holes, DNA replication, the Roman Empire..."
                  className="w-full bg-cinema-black border border-cinema-border rounded-xl px-4 py-3 text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold transition-colors text-base"
                  autoFocus
                />
              </div>
              <div className="space-y-2">
                <label className="text-xs font-semibold text-cinema-gold uppercase tracking-widest">
                  Your Name{" "}
                  <span className="text-cinema-muted font-normal normal-case">
                    (optional — narrator will address you)
                  </span>
                </label>
                <input
                  type="text"
                  value={userName}
                  onChange={(e) => setUserName(e.target.value)}
                  onKeyDown={(e) =>
                    e.key === "Enter" && startDocumentary()
                  }
                  placeholder="Alex"
                  className="w-full bg-cinema-black border border-cinema-border rounded-xl px-4 py-3 text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold transition-colors text-base"
                />
              </div>

              <button
                onClick={startDocumentary}
                disabled={!topic.trim()}
                className="w-full py-4 rounded-xl font-semibold text-base tracking-wide transition-all duration-200
                  bg-cinema-gold text-cinema-black hover:bg-cinema-gold-light disabled:opacity-40 disabled:cursor-not-allowed
                  active:scale-98"
              >
                ▶ Produce My Documentary
              </button>
            </div>

            <div className="mt-8 grid grid-cols-3 gap-4 text-center">
              {[
                { icon: "🎬", label: "Live Narration", desc: "Gemini Live API" },
                { icon: "🎥", label: "Cinematic Video", desc: "Veo Generation" },
                { icon: "🎵", label: "Original Score", desc: "Lyria Music" },
              ].map((f) => (
                <div
                  key={f.label}
                  className="p-4 rounded-xl bg-cinema-card border border-cinema-border"
                >
                  <div className="text-2xl mb-2">{f.icon}</div>
                  <div className="text-xs font-semibold text-cinema-text">
                    {f.label}
                  </div>
                  <div className="text-xs text-cinema-muted mt-1">{f.desc}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── LOADING ── */}
      {phase === "loading" && (
        <div className="flex-1 flex items-center justify-center pt-20">
          <div className="text-center space-y-6 animate-fade-in max-w-md px-6">
            <div className="w-16 h-16 border-2 border-cinema-gold border-t-transparent rounded-full animate-spin mx-auto" />
            <div>
              <h2 className="text-xl font-semibold text-cinema-gold mb-2">
                Producing Your Documentary
              </h2>
              <p className="text-cinema-muted text-sm">
                {`"${topic}"`}
              </p>
            </div>
            <div
              ref={statusLogRef}
              className="status-log text-left bg-cinema-card border border-cinema-border rounded-xl p-4 h-48 overflow-y-auto space-y-1"
            >
              {statusLog.map((s, i) => (
                <div
                  key={i}
                  className="text-xs text-cinema-muted font-mono leading-relaxed"
                >
                  {s}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── PLAYING: Cinema View ── */}
      {phase === "playing" && (
        <div className="flex-1 flex flex-col pt-12">
          {/* Beat progress bar */}
          <div className="px-4 py-2 flex gap-1">
            {Array.from({ length: totalBeats }).map((_, i) => (
              <div
                key={i}
                className={`flex-1 h-1 rounded-full beat-segment transition-all duration-500 ${
                  i < currentBeat
                    ? "bg-cinema-gold"
                    : i === currentBeat
                    ? "bg-cinema-gold-light"
                    : "bg-cinema-border"
                }`}
                title={BEAT_LABELS[i]}
              />
            ))}
          </div>

          {/* Cinema frame + visual */}
          <div className="cinema-frame mx-4 rounded-xl overflow-hidden bg-black relative scanlines">
            {/* Vignette */}
            <div className="absolute inset-0 vignette z-10" />

            {/* Loading skeleton — shown while waiting for first scene image */}
            {!currentVisual && (
              <div className="absolute inset-0 flex items-center justify-center z-5 bg-cinema-black">
                <div className="text-center space-y-4">
                  <div className="w-12 h-12 border-2 border-cinema-gold/40 border-t-cinema-gold rounded-full animate-spin mx-auto" />
                  <p className="text-cinema-gold text-sm font-medium">
                    Composing opening scene...
                  </p>
                  <p className="text-cinema-muted text-xs max-w-xs">
                    The narrator waits for the first frame before speaking
                  </p>
                </div>
              </div>
            )}

            {/* Video display */}
            {currentVisual?.type === "video" && (
              <video
                key={`video-${currentVisual.beatIndex}`}
                ref={videoRef}
                className="w-full h-full object-cover animate-fade-in"
                muted
                loop
                playsInline
                autoPlay
              />
            )}

            {/* Image display — with Ken Burns pan-zoom */}
            {currentVisual?.type === "image" && (
              <div
                key={`${currentVisual.beatIndex}-${currentVisual.data.slice(0, 8)}`}
                className="absolute inset-0 overflow-hidden"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={`data:${currentVisual.mimeType};base64,${currentVisual.data}`}
                  alt="Documentary visual"
                  className={`w-full h-full object-cover ken-burns-${currentBeat % 8}`}
                  style={{ "--kb-duration": `${beatDuration + 4}s` } as React.CSSProperties}
                />
              </div>
            )}

            {/* Beat label overlay (top-left) */}
            {currentBeat >= 0 && (
              <div className="absolute top-4 left-4 z-20">
                <span className="text-xs text-cinema-gold font-semibold uppercase tracking-widest bg-black/60 px-2 py-1 rounded">
                  {BEAT_LABELS[currentBeat] || `Beat ${currentBeat + 1}`}
                </span>
              </div>
            )}

            {/* Audio indicator (top-right) */}
            <div className="absolute top-4 right-4 z-20 flex items-center gap-2">
              {bgmActive && (
                <span className="text-xs text-cinema-muted bg-black/60 px-2 py-1 rounded flex items-center gap-1">
                  <span className="w-1.5 h-1.5 bg-cinema-gold rounded-full recording-dot" />
                  LIVE
                </span>
              )}
            </div>
          </div>

          {/* Subtitles */}
          <div
            ref={subtitleScrollRef}
            className="mx-4 mt-3 min-h-[4.5rem] max-h-24 overflow-hidden flex flex-col justify-end"
          >
            {subtitles.slice(-3).map((line, i) => (
              <p
                key={i}
                className={`subtitle-text text-center ${
                  i === subtitles.slice(-3).length - 1
                    ? "text-white"
                    : "text-cinema-muted"
                }`}
              >
                {line}
              </p>
            ))}
          </div>

          {/* Controls row */}
          <div className="flex items-center gap-3 px-4 py-3">
            {/* Mic button */}
            <button
              onClick={toggleMic}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                micEnabled
                  ? "bg-red-600 text-white hover:bg-red-700"
                  : "bg-cinema-card border border-cinema-border text-cinema-text hover:border-cinema-gold"
              }`}
            >
              {micEnabled ? (
                <>
                  <span className="w-2 h-2 bg-white rounded-full recording-dot" />
                  Stop Mic
                </>
              ) : (
                <>🎙 Speak</>
              )}
            </button>

            {/* Chat input */}
            <input
              ref={chatInputRef}
              type="text"
              placeholder='Ask or command: "show me the core" or "zoom in"...'
              className="flex-1 bg-cinema-card border border-cinema-border rounded-lg px-3 py-2 text-sm text-cinema-text placeholder-cinema-muted focus:outline-none focus:border-cinema-gold transition-colors"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  sendChat((e.target as HTMLInputElement).value);
                  (e.target as HTMLInputElement).value = "";
                }
              }}
            />

            {/* Status mini-log */}
            <div className="hidden md:block text-xs text-cinema-muted truncate max-w-[200px]">
              {statusLog[statusLog.length - 1] || ""}
            </div>
          </div>

          {/* Status log (expandable) */}
          <div
            ref={statusLogRef}
            className="status-log mx-4 mb-4 text-left bg-cinema-card/50 border border-cinema-border/50 rounded-lg p-3 h-20 overflow-y-auto"
          >
            {statusLog.slice(-20).map((s, i) => (
              <div
                key={i}
                className="text-xs text-cinema-muted font-mono leading-relaxed"
              >
                {s}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── QUIZ ── */}
      {phase === "quiz" && quiz && (
        <QuizView
          quiz={quiz}
          onAnswer={answerQuiz}
          onNext={nextQuizQuestion}
          onFinish={() => setPhase("done")}
        />
      )}

      {/* ── DONE ── */}
      {phase === "done" && quiz && (
        <DoneView
          quiz={quiz}
          topic={topic}
          onRestart={() => {
            setPhase("idle");
            setTopic("");
            setQuiz(null);
          }}
        />
      )}
    </div>
  );
}

// ─── Quiz Component ───────────────────────────────────────────────────────────

function QuizView({
  quiz,
  onAnswer,
  onNext,
  onFinish,
}: {
  quiz: QuizState;
  onAnswer: (letter: string) => void;
  onNext: () => void;
  onFinish: () => void;
}) {
  const { questions, currentIdx, answers, score, showExplanation } = quiz;
  const q = questions[currentIdx];
  const userAnswer = answers[currentIdx];
  const isLast = currentIdx === questions.length - 1;

  const letters = ["A", "B", "C", "D"];

  return (
    <div className="flex-1 flex items-center justify-center p-6 pt-20 animate-slide-up">
      <div className="w-full max-w-2xl">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <p className="text-xs text-cinema-gold uppercase tracking-widest font-semibold">
              Knowledge Check
            </p>
            <h2 className="text-cinema-text font-semibold mt-1">
              {quiz.topic}
            </h2>
          </div>
          <div className="text-right">
            <p className="text-xs text-cinema-muted">
              Question {currentIdx + 1} of {questions.length}
            </p>
            <p className="text-cinema-gold font-bold text-lg">{score} pts</p>
          </div>
        </div>

        {/* Progress dots */}
        <div className="flex gap-2 mb-8">
          {questions.map((_, i) => (
            <div
              key={i}
              className={`flex-1 h-1.5 rounded-full transition-all duration-300 ${
                i < currentIdx
                  ? answers[i] === questions[i].correct
                    ? "bg-green-500"
                    : "bg-red-500"
                  : i === currentIdx
                  ? "bg-cinema-gold"
                  : "bg-cinema-border"
              }`}
            />
          ))}
        </div>

        {/* Question card */}
        <div className="bg-cinema-card border border-cinema-border rounded-2xl p-8 mb-6">
          <p className="text-lg font-semibold text-cinema-text leading-relaxed mb-6">
            {q.question}
          </p>

          <div className="space-y-3">
            {q.options.map((option, i) => {
              const letter = letters[i];
              const isChosen = userAnswer === letter;
              const isCorrect = letter === q.correct;

              let cls = "quiz-option border rounded-xl px-4 py-3 flex items-center gap-3";
              if (!showExplanation) {
                cls += " border-cinema-border text-cinema-text cursor-pointer hover:border-cinema-gold hover:bg-cinema-gold/5";
              } else if (isCorrect) {
                cls += " correct border-green-500 bg-green-500/10 text-green-400";
              } else if (isChosen && !isCorrect) {
                cls += " wrong border-red-500 bg-red-500/8 text-red-400";
              } else {
                cls += " border-cinema-border text-cinema-muted opacity-60 answered";
              }

              return (
                <div
                  key={i}
                  className={cls}
                  onClick={() => !showExplanation && onAnswer(letter)}
                >
                  <span
                    className={`w-7 h-7 flex-shrink-0 rounded-lg border flex items-center justify-center text-xs font-bold ${
                      showExplanation && isCorrect
                        ? "border-green-500 bg-green-500 text-white"
                        : showExplanation && isChosen && !isCorrect
                        ? "border-red-500 bg-red-500 text-white"
                        : "border-cinema-border text-cinema-muted"
                    }`}
                  >
                    {letter}
                  </span>
                  <span className="text-sm leading-relaxed">{option}</span>
                </div>
              );
            })}
          </div>

          {/* Explanation */}
          {showExplanation && (
            <div className="mt-6 p-4 bg-cinema-black/50 border border-cinema-border/50 rounded-xl animate-slide-up">
              <p className="text-xs text-cinema-gold uppercase tracking-widest font-semibold mb-2">
                {userAnswer === q.correct ? "✓ Correct!" : "✗ Incorrect"}
              </p>
              <p className="text-sm text-cinema-muted leading-relaxed">
                {q.explanation}
              </p>
            </div>
          )}
        </div>

        {showExplanation && (
          <button
            onClick={isLast ? onFinish : onNext}
            className="w-full py-4 rounded-xl font-semibold bg-cinema-gold text-cinema-black hover:bg-cinema-gold-light transition-colors"
          >
            {isLast ? "See Final Score →" : "Next Question →"}
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Done Screen ──────────────────────────────────────────────────────────────

function DoneView({
  quiz,
  topic,
  onRestart,
}: {
  quiz: QuizState;
  topic: string;
  onRestart: () => void;
}) {
  const { score, questions } = quiz;
  const pct = Math.round((score / questions.length) * 100);
  const grade =
    pct === 100
      ? { label: "Perfect Score!", color: "text-cinema-gold", emoji: "🏆" }
      : pct >= 80
      ? { label: "Excellent!", color: "text-green-400", emoji: "🎉" }
      : pct >= 60
      ? { label: "Well Done!", color: "text-blue-400", emoji: "👍" }
      : { label: "Keep Exploring!", color: "text-cinema-muted", emoji: "🔭" };

  return (
    <div className="flex-1 flex items-center justify-center p-6 pt-20 animate-fade-in">
      <div className="w-full max-w-md text-center space-y-8">
        <div>
          <div className="text-6xl mb-4">{grade.emoji}</div>
          <h2 className={`text-3xl font-bold ${grade.color} mb-2`}>
            {grade.label}
          </h2>
          <p className="text-cinema-muted">
            You completed the documentary on{" "}
            <span className="text-cinema-text">{topic}</span>
          </p>
        </div>

        <div className="bg-cinema-card border border-cinema-border rounded-2xl p-8">
          <div className="text-6xl font-bold text-cinema-gold mb-2">
            {score}/{questions.length}
          </div>
          <p className="text-cinema-muted text-sm">{pct}% correct</p>

          {/* Per-question summary */}
          <div className="mt-6 space-y-2">
            {questions.map((q, i) => (
              <div
                key={i}
                className="flex items-center gap-3 text-sm text-left"
              >
                <span
                  className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${
                    quiz.answers[i] === q.correct
                      ? "bg-green-500 text-white"
                      : "bg-red-500 text-white"
                  }`}
                >
                  {quiz.answers[i] === q.correct ? "✓" : "✗"}
                </span>
                <span className="text-cinema-muted truncate">{q.question}</span>
              </div>
            ))}
          </div>
        </div>

        <button
          onClick={onRestart}
          className="w-full py-4 rounded-xl font-semibold bg-cinema-card border border-cinema-border text-cinema-text hover:border-cinema-gold transition-colors"
        >
          ◈ Create Another Documentary
        </button>
      </div>
    </div>
  );
}

// ─── Utility ─────────────────────────────────────────────────────────────────

function base64ToBlob(base64: string, mimeType: string): Blob {
  const byteCharacters = atob(base64);
  const byteNumbers = new Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  return new Blob([new Uint8Array(byteNumbers)], { type: mimeType });
}
