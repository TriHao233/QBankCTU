/* ═══════════════════════════════════════════════════════════════
   KnowledgePipelineSection
   ───────────────────────────────────────────────────────────────
   Owns everything the 3D scene should not know about:

   · Capability gates — reduced motion, viewport width, WebGL
     support. The WebGL bundle is only imported when all three pass.
   · Visibility — the canvas stops rendering frames when the
     section scrolls out of view (frameloop="never").
   · The stage list — real HTML, keyboard focusable, and the single
     source of the stage copy. Hovering or focusing a stage drives
     the highlight in whichever visual is mounted.
   ═══════════════════════════════════════════════════════════════ */

import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import PipelineDiagram2D from './PipelineDiagram2D';

const PIPELINE_3D_ENABLED = import.meta.env.VITE_PIPELINE_3D === 'true';
const KnowledgePipeline3D = PIPELINE_3D_ENABLED
  ? lazy(() => import('./KnowledgePipeline3D'))
  : null;

export const PIPELINE_STAGES = [
  {
    id: 'document',
    label: 'Tài liệu',
    detail: 'Giáo trình, bài giảng, đề cương PDF/DOCX. Bản scan đi qua OCR trước khi vào pipeline.',
  },
  {
    id: 'rag',
    label: 'RAG',
    detail: 'Văn bản được chunking, nhúng vector và lập chỉ mục để truy xuất đúng ngữ cảnh nguồn.',
  },
  {
    id: 'llm',
    label: 'LLM',
    detail: 'Mô hình ngôn ngữ lớn sinh câu hỏi từ ngữ cảnh đã truy xuất, không sinh từ trí nhớ mô hình.',
  },
  {
    id: 'draft',
    label: 'Câu hỏi nháp',
    detail: 'Kết quả vào trạng thái Nháp kèm loại câu hỏi và cấp độ Bloom — chưa phải dữ liệu chính thức.',
  },
  {
    id: 'review',
    label: 'Kiểm duyệt',
    detail: 'Người duyệt đối chiếu nguồn rồi phê duyệt, hoặc trả lại để giảng viên chỉnh sửa.',
  },
  {
    id: 'moodle',
    label: 'Moodle',
    detail: 'Câu hỏi đã duyệt được chuyển đổi sang GIFT/XML và đồng bộ về ngân hàng câu hỏi Moodle.',
  },
];

function detectWebGL() {
  try {
    const canvas = document.createElement('canvas');
    return Boolean(
      window.WebGLRenderingContext
      && (canvas.getContext('webgl') || canvas.getContext('experimental-webgl')),
    );
  } catch {
    return false;
  }
}

export default function KnowledgePipelineSection() {
  const [activeIndex, setActiveIndex] = useState(null);
  const [canRender3D, setCanRender3D] = useState(false);
  const [visible, setVisible] = useState(false);
  const [ready, setReady] = useState(false);
  const [contextLost, setContextLost] = useState(false);
  const stageRef = useRef(null);

  // ── Capability gate ────────────────────────────────────────────
  useEffect(() => {
    const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    const widthQuery = window.matchMedia('(min-width: 768px)');

    const evaluate = () => {
      setCanRender3D(
        PIPELINE_3D_ENABLED
        && !motionQuery.matches
        && widthQuery.matches
        && detectWebGL(),
      );
    };

    evaluate();
    motionQuery.addEventListener('change', evaluate);
    widthQuery.addEventListener('change', evaluate);
    return () => {
      motionQuery.removeEventListener('change', evaluate);
      widthQuery.removeEventListener('change', evaluate);
    };
  }, []);

  // ── Make R3F pick up its initial size under StrictMode ─────────
  // The app mounts inside <React.StrictMode> (src/main.jsx), whose
  // double-invoked effects make react-use-measure — used by R3F
  // internally — miss its first measurement. The canvas then stays
  // at the 300x150 HTML default forever. react-use-measure also
  // listens for window resize, so one synthetic event makes it
  // re-measure. The canvas arrives asynchronously (lazy chunk), so
  // poll briefly for it and stop as soon as it reports a real size.
  useEffect(() => {
    if (!canRender3D || ready) return undefined;
    let tries = 0;
    const timer = window.setInterval(() => {
      tries += 1;
      if (stageRef.current?.querySelector('canvas')) {
        window.dispatchEvent(new Event('resize'));
      }
      if (tries > 25) window.clearInterval(timer);
    }, 120);
    return () => window.clearInterval(timer);
  }, [canRender3D, ready]);

  // ── Stop rendering frames when the section is off screen ───────
  useEffect(() => {
    const node = stageRef.current;
    if (!node) return undefined;
    const observer = new IntersectionObserver(
      ([entry]) => setVisible(entry.isIntersecting),
      { rootMargin: '160px 0px' },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return (
    <section className="pipeline" id="pipeline" aria-labelledby="pipeline-heading">
      <div className="container">
        <div className="pipeline-head">
          <h2 className="pipeline-heading" id="pipeline-heading">
            Sáu chặng xử lý tri thức trước khi thành câu hỏi
          </h2>
          <p className="pipeline-lede">
            Không có bước nào bị bỏ qua và không có câu hỏi nào tự động vào ngân hàng.
            Di chuột hoặc dùng phím Tab qua từng chặng để xem hệ thống làm gì ở đó.
          </p>
        </div>

        <div className="pipeline-stage" ref={stageRef} data-3d={ready ? 'ready' : 'pending'}>
          {canRender3D && !contextLost ? (
            <Suspense fallback={<PipelineDiagram2D activeIndex={activeIndex} />}>
              <KnowledgePipeline3D
                activeIndex={activeIndex}
                paused={!visible}
                onReady={(state) => {
                  setReady(true);
                  // A lost GPU context leaves the canvas blank with no
                  // error. Drop to the 2D diagram instead of showing a
                  // hole — this fires on driver resets and when mobile
                  // browsers reclaim backgrounded contexts.
                  state.gl.domElement.addEventListener(
                    'webglcontextlost',
                    () => setContextLost(true),
                    { once: true },
                  );
                }}
              />
            </Suspense>
          ) : (
            <PipelineDiagram2D activeIndex={activeIndex} />
          )}
        </div>

        <ol className="pipeline-stages">
          {PIPELINE_STAGES.map((stage, i) => (
            <li key={stage.id}>
              <button
                type="button"
                className={`pipeline-stage-btn${activeIndex === i ? ' is-active' : ''}`}
                aria-pressed={activeIndex === i}
                onMouseEnter={() => setActiveIndex(i)}
                onMouseLeave={() => setActiveIndex(null)}
                onFocus={() => setActiveIndex(i)}
                onBlur={() => setActiveIndex(null)}
                /* Not a toggle: a real click is preceded by mouseenter,
                   which already set activeIndex to i — a toggle against
                   that stale-looking value cancels itself back to null
                   on every click. Just set; mouseleave/blur clears it,
                   same as hover. */
                onClick={() => setActiveIndex(i)}
              >
                <span className="pipeline-stage-num">{String(i + 1).padStart(2, '0')}</span>
                <span className="pipeline-stage-label">{stage.label}</span>
                <span className="pipeline-stage-detail">{stage.detail}</span>
              </button>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
