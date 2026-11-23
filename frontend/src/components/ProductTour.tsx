import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ArrowRight, Compass, MousePointerClick, Send, Sparkles, X } from "lucide-react";
import { TOUR_CHAPTERS, TOUR_STEPS, closeWrikeDialog, type TourStep } from "./tourSteps";
import "./product-tour.css";

/**
 * Animated guided tour of the user dashboard, built like the CSR dashboard's
 * tour: a chooser, then a moving pointer that glides to each control, clicks
 * where needed, and a spotlight + explanation card on the real page.
 *
 * Mount <GuidedTour /> once (the user dashboard does). Anything can start it
 * with <TourButton /> or <TourLink />, or with a #tour link, for example
 * https://leads.catalysts.org/#tour. The admin dashboard links to
 * ?view=user#tour.
 *
 * Safety: while the tour runs a transparent shield stops the visitor's own
 * clicks, and the script (tourSteps.ts) never clicks "Yes, create task",
 * Save lead or Mark reviewed, so nothing is sent to Wrike or saved.
 */

const OPEN_EVENT = "lop:open-tour";
const SEEN_KEY = "lop.tour.seen.v1";
const PAD = 8;
const CARD_W = 370;

type Mode = "full" | "wrike";
interface Box { top: number; left: number; width: number; height: number }

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

function resolveTarget(target: TourStep["target"]): Element | null {
  if (!target) return null;
  try { return typeof target === "function" ? target() : document.querySelector(target); } catch { return null; }
}

/** Wait until smooth scrolling has stopped moving the element. */
async function waitStill(element: Element, timeout = 1500) {
  let last = element.getBoundingClientRect();
  const started = Date.now();
  while (Date.now() - started < timeout) {
    await sleep(90);
    const now = element.getBoundingClientRect();
    if (Math.abs(now.top - last.top) < 1 && Math.abs(now.left - last.left) < 1) return;
    last = now;
  }
}

async function waitFor(find: () => Element | null, timeout = 5000): Promise<Element | null> {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const element = find();
    if (element) {
      const r = element.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return element;
    }
    await sleep(120);
  }
  return null;
}

export function openTour() { window.dispatchEvent(new Event(OPEN_EVENT)); }

export function TourButton({ className }: { className?: string }) {
  return <button type="button" className={className ?? "gt-launch"} onClick={openTour}>
    <Compass size={14} aria-hidden="true" /> Take a tour
  </button>;
}

/** "How to use this platform · Take a tour". With href it is a plain link. */
export function TourLink({ href }: { href?: string }) {
  const inner = <>How to use this platform · <span>Take a tour</span></>;
  return href
    ? <a className="gt-link" href={href}>{inner}</a>
    : <button type="button" className="gt-link" onClick={openTour}>{inner}</button>;
}

export function GuidedTour() {
  const [chooserOpen, setChooserOpen] = useState(false);
  const [steps, setSteps] = useState<TourStep[] | null>(null);
  const [index, setIndex] = useState(0);
  const [box, setBox] = useState<Box | null>(null);
  const [found, setFound] = useState(true);
  const [cursor, setCursor] = useState({ x: -80, y: -80, visible: false, clicking: false });
  const [ready, setReady] = useState(false);
  const [cardH, setCardH] = useState(220);
  const runId = useRef(0);
  const targetRef = useRef<Element | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  // Open from the buttons, from a #tour link, and once on a first visit.
  useEffect(() => {
    const open = () => setChooserOpen(true);
    window.addEventListener(OPEN_EVENT, open);
    let timer = 0;
    if (window.location.hash === "#tour") {
      history.replaceState({}, "", `${window.location.pathname}${window.location.search}`);
      timer = window.setTimeout(open, 600);
    } else {
      try {
        if (!window.localStorage.getItem(SEEN_KEY)) {
          window.localStorage.setItem(SEEN_KEY, "1");
          timer = window.setTimeout(open, 1500);
        }
      } catch { /* storage blocked: no automatic offer */ }
    }
    return () => { window.removeEventListener(OPEN_EVENT, open); clearTimeout(timer); };
  }, []);

  const startTour = useCallback((mode: Mode) => {
    const list = mode === "wrike"
      ? TOUR_STEPS.filter(s => s.chapter === "wrike" || s.chapter === "finish" || s.id === "open-lead")
      : TOUR_STEPS;
    setChooserOpen(false);
    setReady(false);
    setIndex(0);
    setSteps(list);
  }, []);

  const endTour = useCallback(() => {
    runId.current += 1;
    setSteps(null);
    setBox(null);
    setReady(false);
    setCursor(c => ({ ...c, visible: false }));
    closeWrikeDialog();
  }, []);

  const measure = useCallback(() => {
    const element = targetRef.current;
    if (!element || !element.isConnected) { setBox(null); return; }
    const r = element.getBoundingClientRect();
    const next = { top: r.top - PAD, left: r.left - PAD, width: r.width + PAD * 2, height: r.height + PAD * 2 };
    setBox(prev => prev && Math.abs(prev.top - next.top) < 0.5 && Math.abs(prev.left - next.left) < 0.5
      && Math.abs(prev.width - next.width) < 0.5 && Math.abs(prev.height - next.height) < 0.5 ? prev : next);
  }, []);

  // Run the current step: prepare, find, scroll, glide the pointer, click.
  useEffect(() => {
    if (!steps) return;
    const step = steps[index];
    if (!step) return;
    const id = ++runId.current;
    const alive = () => runId.current === id;
    setReady(false);
    (async () => {
      if (step.before) {
        try { await step.before(); } catch { /* show the step without the prep */ }
        await sleep(220);
        if (!alive()) return;
      }
      const element = step.target ? await waitFor(() => resolveTarget(step.target), 3500) : null;
      if (!alive()) return;
      targetRef.current = element;
      setFound(!step.target || !!element);
      if (element) {
        element.scrollIntoView({ behavior: "smooth", block: step.block ?? "center", inline: "nearest" });
        await sleep(200);
        await waitStill(element);
        if (!alive()) return;
        measure();
      } else setBox(null);

      const clickEl = step.click ? await waitFor(() => resolveTarget(step.click), 2500) : element;
      if (!alive()) return;
      if (clickEl) {
        if (clickEl !== element) {
          clickEl.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
          await waitStill(clickEl);
          if (!alive()) return;
        }
        const r = clickEl.getBoundingClientRect();
        const x = r.width > 320 ? r.left + 48 : r.left + r.width / 2;
        const y = r.height > 160 ? r.top + 40 : r.top + r.height / 2;
        setCursor({ x, y, visible: true, clicking: false });
        await sleep(820);
        if (!alive()) return;
        if (step.click) {
          setCursor(c => ({ ...c, clicking: true }));
          await sleep(180);
          if (step.action) step.action(); else (clickEl as HTMLElement).click();
          await sleep(260);
          setCursor(c => ({ ...c, clicking: false }));
          await sleep(step.settle ?? 500);
          if (!alive()) return;
          const after = step.afterClick ?? step.target;
          if (after) {
            const again = await waitFor(() => resolveTarget(after), 2500);
            if (!alive()) return;
            targetRef.current = again;
            setFound(!!again);
            measure();
          }
        }
      } else setCursor(c => ({ ...c, visible: false }));
      setReady(true);
    })();
  }, [steps, index, measure]);

  // Keep the spotlight glued to its element while the page moves.
  useEffect(() => {
    if (!steps) return;
    let timer = 0;
    const loop = () => { measure(); timer = window.setTimeout(loop, 120); };
    loop();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [steps, measure]);

  useEffect(() => {
    const node = cardRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const h = node.getBoundingClientRect().height;
      setCardH(prev => (Math.abs(prev - h) > 1 ? h : prev));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [steps]);

  const next = useCallback(() => {
    if (!steps) return;
    // ready goes false in the same render as the index, so Next cannot be
    // pressed again before the new step has finished its demo.
    if (index >= steps.length - 1) endTour(); else { setReady(false); setIndex(i => i + 1); }
  }, [steps, index, endTour]);
  const back = useCallback(() => { setReady(false); setIndex(i => Math.max(0, i - 1)); }, []);

  useEffect(() => {
    if (!steps && !chooserOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { if (steps) endTour(); else setChooserOpen(false); }
      else if (steps && (event.key === "ArrowRight" || event.key === "Enter")) { event.preventDefault(); next(); }
      else if (steps && event.key === "ArrowLeft") { event.preventDefault(); back(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [steps, chooserOpen, next, back, endTour]);

  const step = steps?.[index];
  const cardPos = useMemo(() => {
    const vw = window.innerWidth, vh = window.innerHeight;
    const w = Math.min(CARD_W, vw - 24), h = cardH;
    if (!box || !step?.target) return { left: (vw - w) / 2, top: Math.max(24, (vh - h) / 2), w };
    const gap = 16;
    const fits = {
      bottom: box.top + box.height + gap + h < vh - 12,
      top: box.top - gap - h > 12,
      right: box.left + box.width + gap + w < vw - 12,
      left: box.left - gap - w > 12,
    };
    const order = step.placement && step.placement !== "auto"
      ? [step.placement, "bottom", "top", "right", "left"] : ["bottom", "top", "right", "left"];
    const side = order.find(s => fits[s as keyof typeof fits]) ?? "corner";
    let left: number, top: number;
    if (side === "bottom" || side === "top") {
      left = box.left + box.width / 2 - w / 2;
      top = side === "bottom" ? box.top + box.height + gap : box.top - gap - h;
    } else if (side === "right" || side === "left") {
      top = box.top + box.height / 2 - h / 2;
      left = side === "right" ? box.left + box.width + gap : box.left - gap - w;
    } else { left = vw - w - 20; top = vh - h - 20; }
    return { left: Math.max(12, Math.min(left, vw - w - 12)), top: Math.max(12, Math.min(top, vh - h - 12)), w };
  }, [box, step, cardH]);

  if (!steps && !chooserOpen) return null;
  const chapterSteps = steps && step ? steps.filter(s => s.chapter === step.chapter) : [];
  const chapterPos = step ? chapterSteps.indexOf(step) + 1 : 0;
  const body = step ? (!found && step.fallback ? step.fallback : typeof step.body === "function" ? step.body() : step.body) : "";
  const last = !!steps && index >= steps.length - 1;

  return createPortal(<div className="gt-root" role="dialog" aria-modal="true" aria-label="Guided tour">
    {chooserOpen && !steps && <Chooser onClose={() => setChooserOpen(false)} onStart={startTour} />}
    {steps && step && <>
      {/* Click shield: the tour drives the page, the visitor reads. */}
      <div className="gt-shield" />
      {box && step.target ? <div className="gt-spotlight" style={{ top: box.top, left: box.left, width: box.width, height: box.height }} />
        : <div className="gt-dim" />}
      <div className={`gt-cursor${cursor.visible ? " show" : ""}${cursor.clicking ? " clicking" : ""}`}
           style={{ transform: `translate(${cursor.x}px, ${cursor.y}px)` }} aria-hidden="true">
        <svg width="26" height="30" viewBox="0 0 26 30">
          <path d="M2 2 L2 24 L8 18.5 L12.5 28 L16.5 26.2 L12 17 L20.5 17 Z" fill="var(--gt-accent)" stroke="#fff" strokeWidth="2" strokeLinejoin="round" />
        </svg>
        <span className="gt-ripple" />
      </div>
      <div ref={cardRef} className={`gt-card${ready ? " ready" : ""}`} style={{ left: cardPos.left, top: cardPos.top, width: cardPos.w }} aria-live="polite">
        <div className="gt-card-top">
          <span className="gt-chapter">{TOUR_CHAPTERS[step.chapter].label}{chapterSteps.length > 1 ? ` · ${chapterPos} of ${chapterSteps.length}` : ""}</span>
          <button type="button" className="gt-x" onClick={endTour} aria-label="Close tour"><X size={15} /></button>
        </div>
        <div className="gt-title">{step.title}</div>
        <div className="gt-body">{body}</div>
        {step.watch && found && <div className="gt-watch"><span className="gt-pulse" />{step.watch}</div>}
        <div className="gt-progress"><span style={{ width: `${((index + 1) / steps.length) * 100}%` }} /></div>
        <div className="gt-actions">
          <button type="button" className="gt-skip" onClick={endTour}>{last ? "Close" : "Skip tour"}</button>
          <span className="gt-count">{index + 1} / {steps.length}</span>
          <button type="button" className="gt-btn ghost" onClick={back} disabled={index === 0} aria-label="Back"><ArrowLeft size={14} /></button>
          <button type="button" className="gt-btn" onClick={next} disabled={!ready && !last}>
            {last ? "Finish" : "Next"}{!last && <ArrowRight size={14} />}
          </button>
        </div>
      </div>
    </>}
  </div>, document.body);
}

function Chooser({ onClose, onStart }: { onClose: () => void; onStart: (mode: Mode) => void }) {
  const wrikeCount = TOUR_STEPS.filter(s => s.chapter === "wrike" || s.chapter === "finish" || s.id === "open-lead").length;
  const options: { mode: Mode; icon: ReactNode; title: string; text: string; meta: string }[] = [
    { mode: "full", icon: <Compass size={20} />, title: "Full tour",
      text: "Search, filters, one real lead from start to finish, and pushing it to Wrike.",
      meta: `${TOUR_STEPS.length} steps · about 3 minutes` },
    { mode: "wrike", icon: <Send size={20} />, title: "How to push a lead to Wrike",
      text: "Where the Wrike panel is, adding an assignee or leaving it empty, and the final check.",
      meta: `${wrikeCount} steps · about 1 minute` },
  ];
  return <div className="gt-chooser-wrap" onClick={onClose}>
    <div className="gt-chooser" onClick={event => event.stopPropagation()}>
      <button type="button" className="gt-x gt-chooser-x" onClick={onClose} aria-label="Close"><X size={16} /></button>
      <div className="gt-badge"><Sparkles size={14} /> Guided tour</div>
      <h2 className="gt-chooser-title">Welcome to the Lead Scanning Platform</h2>
      <p className="gt-chooser-text">A moving pointer shows you where to click and opens things for you on one real lead.
        Nothing is saved, sent or pushed to Wrike during the tour.</p>
      <div className="gt-options">
        {options.map(o => <button key={o.mode} type="button" className="gt-option" onClick={() => onStart(o.mode)}>
          <span className="gt-option-icon">{o.icon}</span>
          <span className="gt-option-text"><strong>{o.title}</strong><span>{o.text}</span><em>{o.meta}</em></span>
          <ArrowRight size={16} className="gt-option-go" />
        </button>)}
      </div>
      <div className="gt-keys"><MousePointerClick size={13} /> Use <kbd>→</kbd> / <kbd>←</kbd> to move, <kbd>Esc</kbd> to stop at any time</div>
    </div>
  </div>;
}
