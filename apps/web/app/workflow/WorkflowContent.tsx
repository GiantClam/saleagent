"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";

type StoryboardItem = {
  idx: number;
  desc: string;
  begin_s: number;
  end_s: number;
  keyframes?: {
    in?: string;
    out?: string;
  };
};

type WorkflowStep = "collect" | "planning" | "keyframes" | "confirm" | "generating" | "stitching" | "done";

export function WorkflowContent() {
  const router = useRouter();
  const apiUrl = process.env.NEXT_PUBLIC_AGENT_URL || "https://api.aimarketingsite.com";

  // 步骤1：收集用户输入
  const [step, setStep] = useState<WorkflowStep>("collect");
  const [subject, setSubject] = useState("");
  const [totalDuration, setTotalDuration] = useState(10.0);
  const [styles, setStyles] = useState<string[]>([]);
  const [imageControl, setImageControl] = useState(false);
  const [productImageUrl, setProductImageUrl] = useState("");
  const [useVoiceAgent, setUseVoiceAgent] = useState(false);
  const [useBgmAgent, setUseBgmAgent] = useState(false);
  const [muteModelAudio, setMuteModelAudio] = useState(false);
  const [clipCount, setClipCount] = useState(4);

  // 步骤2：分镜方案
  const [storyboards, setStoryboards] = useState<StoryboardItem[]>([]);
  const [planning, setPlanning] = useState(false);

  // 步骤3：关键帧
  const [generatingKeyframes, setGeneratingKeyframes] = useState(false);

  // 步骤4：确认与生成
  const [runId, setRunId] = useState<string | null>(null);
  const [clipResults, setClipResults] = useState<Array<{ idx: number; video_url: string; status: string }>>([]);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 步骤5：拼接
  const [finalVideoUrl, setFinalVideoUrl] = useState<string | null>(null);
  const [stitching, setStitching] = useState(false);

  const styleOptions = ["科技感", "温馨", "时尚", "简约", "动感", "专业", "创意", "自然"];

  // 步骤1：提交收集的信息，生成分镜方案
  const handlePlan = async () => {
    if (!subject.trim() || !apiUrl) return;
    setPlanning(true);
    try {
      const res = await fetch(`${apiUrl}/workflow/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal: subject,
          total_duration: totalDuration,
          styles,
          image_control: imageControl,
          num_clips: clipCount,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStoryboards(data.storyboards || []);
      setStep("planning");
    } catch (err) {
      alert(`生成分镜失败：${err}`);
    } finally {
      setPlanning(false);
    }
  };

  const handleRhScenes = async () => {
    if (!apiUrl || !productImageUrl || storyboards.length === 0) return;
    try {
      const res = await fetch(`${apiUrl}/workflow/rh-scenes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image_url: productImageUrl,
          styles,
          total_duration: totalDuration,
          storyboards,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const clips = data.clips || [];
      if (clips.length > 0) {
        setStoryboards(clips);
        setStep("planning");
      }
    } catch (err) {
      alert(`生成场景失败：${err}`);
    }
  };

  // 步骤2：生成关键帧（如果需要）
  const handleGenerateKeyframes = async () => {
    if (!apiUrl || storyboards.length === 0) return;
    setGeneratingKeyframes(true);
    try {
      const res = await fetch(`${apiUrl}/workflow/keyframes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          storyboards,
          image_control: imageControl,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStoryboards(data.storyboards || storyboards);
      setStep("keyframes");
    } catch (err) {
      alert(`生成关键帧失败：${err}`);
    } finally {
      setGeneratingKeyframes(false);
    }
  };

  // 步骤3：确认分镜方案
  const handleConfirm = async () => {
    if (!apiUrl || storyboards.length === 0) return;
    try {
      const res = await fetch(`${apiUrl}/workflow/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          storyboards,
          total_duration: totalDuration,
          styles,
          image_control: imageControl,
          use_voice_agent: useVoiceAgent,
          use_bgm_agent: useBgmAgent,
          mute_model_audio: muteModelAudio,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRunId(data.run_id);
      setStep("confirm");
    } catch (err) {
      alert(`确认失败：${err}`);
    }
  };

  // 步骤4：生成镜头视频（并发最多4个，使用 SSE 实时进度）
  const handleRunClips = async () => {
    if (!apiUrl || !runId || storyboards.length === 0) return;
    setGenerating(true);
    setStep("generating");
    setClipResults([]); // 清空之前的结果

    try {
      const res = await fetch(`${apiUrl}/workflow/run-clips`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "text/event-stream"
        },
        body: JSON.stringify({
          run_id: runId,
          storyboards,
        }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      // 检查是否为 SSE 流
      const contentType = res.headers.get("content-type");
      if (contentType?.includes("text/event-stream") && res.body) {
        // 使用 ReadableStream 解析 SSE
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const data = JSON.parse(line.slice(6));
                try { console.log("[SSE/run-clips]", data); } catch { }
                if (data.type === "progress") {
                  // 更新单个镜头的进度
                  setClipResults((prev) => {
                    const existing = prev.find((c) => c.idx === data.clip.idx);
                    if (existing) {
                      return prev.map((c) =>
                        c.idx === data.clip.idx
                          ? { ...c, status: data.clip.status, video_url: data.clip.video_url }
                          : c
                      );
                    } else {
                      return [...prev, {
                        idx: data.clip.idx,
                        status: data.clip.status,
                        video_url: data.clip.video_url,
                      }];
                    }
                  });
                } else if (data.type === "done") {
                  // 所有镜头完成
                  setClipResults(data.results || []);
                  setGenerating(false);
                }
              } catch (e) {
                console.error("解析 SSE 数据失败:", e);
              }
            }
          }
        }
      } else {
        // 回退到同步方式（兼容旧接口）
        const data = await res.json();
        setClipResults(data.results || data.clips || []);
        setGenerating(false);
      }
    } catch (err) {
      alert(`生成镜头失败：${err}`);
      setGenerating(false);
    }
  };

  // 步骤5：拼接视频
  const handleStitch = async () => {
    if (!apiUrl || !runId || clipResults.length === 0) return;
    setStitching(true);
    setStep("stitching");
    try {
      const segments = clipResults.map((c) => c.video_url);
      const res = await fetch(`${apiUrl}/workflow/stitch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_id: runId,
          segments,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setFinalVideoUrl(data.final_url);
      setStep("done");
    } catch (err) {
      alert(`拼接失败：${err}`);
    } finally {
      setStitching(false);
    }
  };

  // 使用 CrewAI 执行整条工作流（实验）
  const [crewRunning, setCrewRunning] = useState(false);
  const handleCrewRun = async () => {
    if (!apiUrl) return;
    setCrewRunning(true);
    try {
      const res = await fetch(`${apiUrl}/workflow/crew-run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal: subject,
          styles,
          total_duration: totalDuration,
          num_clips: clipCount,
          image_control: imageControl,
          run_id: runId || undefined,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (typeof setError === "function") setError(null);
      if (data.run_id && !runId) {
        setRunId(String(data.run_id));
      }
      // data.result 期望为最终视频 URL（或执行摘要）
      if (typeof data.result === "string" && (data.result.startsWith("http://") || data.result.startsWith("https://"))) {
        setFinalVideoUrl(data.result);
        setStep("done");
      } else {
        // 非 URL 的结果，展示为错误或信息
        if (typeof setError === "function") {
          setError(typeof data.result === "string" ? data.result : "CrewAI 执行完成，但未返回视频链接");
        }
      }
    } catch (err: any) {
      if (typeof setError === "function") {
        setError(`CrewAI 执行失败：${err?.message || String(err)}`);
      }
    } finally {
      setCrewRunning(false);
    }
  };

  // 更新单个分镜描述
  const updateStoryboardDesc = (idx: number, desc: string) => {
    setStoryboards((prev) =>
      prev.map((s) => (s.idx === idx ? { ...s, desc } : s))
    );
  };

  // 更新关键帧
  const updateKeyframe = (idx: number, type: "in" | "out", url: string) => {
    setStoryboards((prev) =>
      prev.map((s) => {
        if (s.idx === idx) {
          return {
            ...s,
            keyframes: { ...(s.keyframes || {}), [type]: url },
          };
        }
        return s;
      })
    );
  };

  // 重新生成单个镜头
  const handleRegenerateClip = async (idx: number) => {
    if (!apiUrl || !runId) return;
    const clip = storyboards.find((s) => s.idx === idx);
    if (!clip) return;
    try {
      const res = await fetch(`${apiUrl}/workflow/run-clips`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_id: runId,
          storyboards: [clip],
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.clips && data.clips.length > 0) {
        setClipResults((prev) =>
          prev.map((c) => (c.idx === idx ? data.clips[0] : c))
        );
      }
    } catch (err) {
      alert(`重新生成失败：${err}`);
    }
  };

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: 24, background: "#fafafa", minHeight: "calc(100vh - 60px)" }}>
      <h1 style={{ fontSize: 28, fontWeight: 700, marginBottom: 32, color: "#1a1a1a" }}>
        多智能体视频生成工作流
      </h1>

      {/* 步骤1：收集信息 */}
      {step === "collect" && (
        <div style={{ background: "white", padding: 32, borderRadius: 12, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 24 }}>步骤 1：完善视频信息</h2>

          <div style={{ marginBottom: 20 }}>
            <label style={{ display: "block", marginBottom: 8, fontSize: 14, fontWeight: 500 }}>产品图片URL *</label>
            <input
              type="url"
              placeholder="https://..."
              value={productImageUrl}
              onChange={(e) => setProductImageUrl(e.target.value)}
              style={{
                width: "100%",
                padding: 12,
                border: "1px solid #d1d5db",
                borderRadius: 8,
                fontSize: 14,
              }}
            />
          </div>

          <div style={{ marginBottom: 12, display: "flex", gap: 16 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <input type="checkbox" checked={useVoiceAgent} onChange={(e) => setUseVoiceAgent(e.target.checked)} />
              <span style={{ fontSize: 14, fontWeight: 500 }}>启用旁白 agent</span>
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <input type="checkbox" checked={useBgmAgent} onChange={(e) => setUseBgmAgent(e.target.checked)} />
              <span style={{ fontSize: 14, fontWeight: 500 }}>启用背景 BGM agent</span>
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <input type="checkbox" checked={muteModelAudio} onChange={(e) => setMuteModelAudio(e.target.checked)} />
              <span style={{ fontSize: 14, fontWeight: 500 }}>禁止模型视频音轨</span>
            </label>
          </div>

          <div style={{ marginBottom: 20 }}>
            <label style={{ display: "block", marginBottom: 8, fontSize: 14, fontWeight: 500 }}>主体目标 *</label>
            <textarea
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="例如：3C新品发布：轻薄长续航"
              style={{
                width: "100%",
                padding: 12,
                border: "1px solid #d1d5db",
                borderRadius: 8,
                fontSize: 14,
                minHeight: 80,
              }}
            />
          </div>

          <div style={{ marginBottom: 20 }}>
            <label style={{ display: "block", marginBottom: 8, fontSize: 14, fontWeight: 500 }}>
              总时长（秒，支持0.1秒精度，最长10秒）*
            </label>
            <input
              type="number"
              step="0.1"
              min="0.1"
              max="10"
              value={totalDuration}
              onChange={(e) => setTotalDuration(parseFloat(e.target.value) || 0)}
              style={{
                width: "100%",
                padding: 12,
                border: "1px solid #d1d5db",
                borderRadius: 8,
                fontSize: 14,
              }}
            />
          </div>

          <div style={{ marginBottom: 20 }}>
            <label style={{ display: "block", marginBottom: 8, fontSize: 14, fontWeight: 500 }}>风格（可多选）</label>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {styleOptions.map((s) => (
                <button
                  key={s}
                  onClick={() => {
                    setStyles((prev) =>
                      prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]
                    );
                  }}
                  style={{
                    padding: "8px 16px",
                    borderRadius: 20,
                    border: `1px solid ${styles.includes(s) ? "#4f46e5" : "#d1d5db"}`,
                    background: styles.includes(s) ? "#e0e7ff" : "white",
                    color: styles.includes(s) ? "#4338ca" : "#374151",
                    fontSize: 13,
                    fontWeight: 500,
                    cursor: "pointer",
                  }}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>

          <div style={{ marginBottom: 20 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={imageControl}
                onChange={(e) => setImageControl(e.target.checked)}
              />
              <span style={{ fontSize: 14, fontWeight: 500 }}>通过图片控制（需要首帧/尾帧）</span>
            </label>
          </div>

          <div style={{ marginBottom: 24 }}>
            <label style={{ display: "block", marginBottom: 8, fontSize: 14, fontWeight: 500 }}>
              镜头数量（1-4个）
            </label>
            <input
              type="number"
              min="1"
              max="4"
              value={clipCount}
              onChange={(e) => setClipCount(Math.max(1, Math.min(4, parseInt(e.target.value) || 1)))}
              style={{
                width: "100%",
                padding: 12,
                border: "1px solid #d1d5db",
                borderRadius: 8,
                fontSize: 14,
              }}
            />
          </div>

          <button
            onClick={handlePlan}
            disabled={!subject.trim() || planning}
            style={{
              padding: "12px 24px",
              borderRadius: 8,
              background: planning ? "#9ca3af" : "#4f46e5",
              color: "white",
              border: 0,
              fontSize: 14,
              fontWeight: 600,
              cursor: planning ? "not-allowed" : "pointer",
            }}
          >
            {planning ? "生成中..." : "生成分镜方案"}
          </button>
        </div>
      )}

      {/* 步骤2：分镜方案确认 */}
      {step === "planning" && storyboards.length > 0 && (
        <div style={{ background: "white", padding: 32, borderRadius: 12, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 24 }}>步骤 2：确认分镜方案</h2>

          {storyboards.map((sb) => (
            <div key={sb.idx} style={{ marginBottom: 24, padding: 20, border: "1px solid #e5e7eb", borderRadius: 8 }}>
              <div style={{ marginBottom: 12 }}>
                <span style={{ fontSize: 12, color: "#6b7280", marginRight: 12 }}>镜头 {sb.idx + 1}</span>
                <span style={{ fontSize: 12, color: "#6b7280" }}>
                  {sb.begin_s.toFixed(1)}s - {sb.end_s.toFixed(1)}s
                </span>
              </div>
              <textarea
                value={sb.desc}
                onChange={(e) => updateStoryboardDesc(sb.idx, e.target.value)}
                style={{
                  width: "100%",
                  padding: 12,
                  border: "1px solid #d1d5db",
                  borderRadius: 8,
                  fontSize: 14,
                  minHeight: 60,
                }}
              />
            </div>
          ))}

          <div style={{ display: "flex", gap: 12 }}>
            <button
              onClick={() => setStep("collect")}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                border: "1px solid #d1d5db",
                background: "white",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              返回修改
            </button>
            {imageControl && (
              <button
                onClick={handleGenerateKeyframes}
                disabled={generatingKeyframes}
                style={{
                  padding: "12px 24px",
                  borderRadius: 8,
                  background: generatingKeyframes ? "#9ca3af" : "#10b981",
                  color: "white",
                  border: 0,
                  fontSize: 14,
                  fontWeight: 600,
                  cursor: generatingKeyframes ? "not-allowed" : "pointer",
                }}
              >
                {generatingKeyframes ? "生成中..." : "生成首尾帧"}
              </button>
            )}
            {productImageUrl && (
              <button
                onClick={handleRhScenes}
                style={{
                  padding: "12px 24px",
                  borderRadius: 8,
                  background: "#10b981",
                  color: "white",
                  border: 0,
                  fontSize: 14,
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                基于图片生成场景并整理为storyboard
              </button>
            )}
            <button
              onClick={handleConfirm}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                background: "#4f46e5",
                color: "white",
                border: 0,
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              确认并继续
            </button>
          </div>
        </div>
      )}

      {/* 步骤3：关键帧编辑（如果启用图控） */}
      {step === "keyframes" && imageControl && storyboards.length > 0 && (
        <div style={{ background: "white", padding: 32, borderRadius: 12, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 24 }}>步骤 3：编辑关键帧</h2>

          {storyboards.map((sb) => (
            <div key={sb.idx} style={{ marginBottom: 24, padding: 20, border: "1px solid #e5e7eb", borderRadius: 8 }}>
              <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>镜头 {sb.idx + 1}</h3>

              <div style={{ marginBottom: 16 }}>
                <label style={{ display: "block", marginBottom: 8, fontSize: 13 }}>首帧</label>
                {sb.keyframes?.in && (
                  <img src={sb.keyframes.in} alt="首帧" style={{ maxWidth: 200, borderRadius: 8, marginBottom: 8 }} />
                )}
                <input
                  type="file"
                  accept="image/*"
                  onChange={async (e) => {
                    if (!apiUrl) return;
                    const f = e.target.files?.[0];
                    if (!f) return;
                    const fd = new FormData();
                    fd.append("file", f);
                    try {
                      const res = await fetch(`${apiUrl}/workflow/upload-image`, { method: "POST", body: fd });
                      if (!res.ok) throw new Error(`HTTP ${res.status}`);
                      const data = await res.json();
                      if (data.fileName) {
                        // RunningHub 返回 fileName，直接作为节点输入
                        updateKeyframe(sb.idx, "in", data.fileName);
                      } else {
                        alert("上传失败");
                      }
                    } catch (err) {
                      alert(`上传失败：${err}`);
                    } finally {
                      e.currentTarget.value = "";
                    }
                  }}
                  style={{ marginBottom: 8 }}
                />
                <input
                  type="url"
                  placeholder="首帧图片URL"
                  value={sb.keyframes?.in || ""}
                  onChange={(e) => updateKeyframe(sb.idx, "in", e.target.value)}
                  style={{
                    width: "100%",
                    padding: 8,
                    border: "1px solid #d1d5db",
                    borderRadius: 6,
                    fontSize: 13,
                  }}
                />
              </div>

              <div>
                <label style={{ display: "block", marginBottom: 8, fontSize: 13 }}>尾帧</label>
                {sb.keyframes?.out && (
                  <img src={sb.keyframes.out} alt="尾帧" style={{ maxWidth: 200, borderRadius: 8, marginBottom: 8 }} />
                )}
                <input
                  type="file"
                  accept="image/*"
                  onChange={async (e) => {
                    if (!apiUrl) return;
                    const f = e.target.files?.[0];
                    if (!f) return;
                    const fd = new FormData();
                    fd.append("file", f);
                    try {
                      const res = await fetch(`${apiUrl}/workflow/upload-image`, { method: "POST", body: fd });
                      if (!res.ok) throw new Error(`HTTP ${res.status}`);
                      const data = await res.json();
                      if (data.fileName) {
                        updateKeyframe(sb.idx, "out", data.fileName);
                      } else {
                        alert("上传失败");
                      }
                    } catch (err) {
                      alert(`上传失败：${err}`);
                    } finally {
                      e.currentTarget.value = "";
                    }
                  }}
                  style={{ marginBottom: 8 }}
                />
                <input
                  type="url"
                  placeholder="尾帧图片URL"
                  value={sb.keyframes?.out || ""}
                  onChange={(e) => updateKeyframe(sb.idx, "out", e.target.value)}
                  style={{
                    width: "100%",
                    padding: 8,
                    border: "1px solid #d1d5db",
                    borderRadius: 6,
                    fontSize: 13,
                  }}
                />
              </div>
            </div>
          ))}

          <div style={{ display: "flex", gap: 12 }}>
            <button
              onClick={() => setStep("planning")}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                border: "1px solid #d1d5db",
                background: "white",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              返回
            </button>
            <button
              onClick={handleConfirm}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                background: "#4f46e5",
                color: "white",
                border: 0,
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              确认并继续
            </button>
          </div>
        </div>
      )}

      {/* 步骤4：生成镜头视频 */}
      {step === "confirm" && runId && (
        <div style={{ background: "white", padding: 32, borderRadius: 12, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 24 }}>步骤 4：生成镜头视频</h2>
          <p style={{ fontSize: 14, color: "#6b7280", marginBottom: 16 }}>
            将并发生成 {storyboards.length} 个镜头视频（最多4个同时进行）
          </p>
          <div style={{ display: "flex", gap: 12 }}>
            <button
              onClick={handleRunClips}
              disabled={generating}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                background: generating ? "#9ca3af" : "#4f46e5",
                color: "white",
                border: 0,
                fontSize: 14,
                fontWeight: 600,
                cursor: generating ? "not-allowed" : "pointer",
              }}
            >
              {generating ? "生成中..." : "开始生成（最多4个并发）"}
            </button>
            <button
              onClick={handleCrewRun}
              disabled={crewRunning}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                background: crewRunning ? "#9ca3af" : "#10b981",
                color: "white",
                border: 0,
                fontSize: 14,
                fontWeight: 600,
                cursor: crewRunning ? "not-allowed" : "pointer",
              }}
              title="使用 CrewAI 执行整条工作流（实验）"
            >
              {crewRunning ? "CrewAI 执行中..." : "使用 CrewAI 执行（实验）"}
            </button>
          </div>
        </div>
      )}

      {/* 步骤5：生成进度与结果 */}
      {step === "generating" && (
        <div style={{ background: "white", padding: 32, borderRadius: 12, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 24 }}>步骤 5：镜头生成进度</h2>

          {/* 总体进度 */}
          {storyboards.length > 0 && (
            <div style={{ marginBottom: 24, padding: 16, background: "#f9fafb", borderRadius: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 600 }}>总体进度</span>
                <span style={{ fontSize: 14, color: "#6b7280" }}>
                  {clipResults.filter((c) => c.status === "succeeded").length} / {storyboards.length}
                </span>
              </div>
              <div style={{ width: "100%", height: 8, background: "#e5e7eb", borderRadius: 4, overflow: "hidden" }}>
                <div
                  style={{
                    width: `${(clipResults.filter((c) => c.status === "succeeded").length / storyboards.length) * 100}%`,
                    height: "100%",
                    background: "#10b981",
                    transition: "width 0.3s",
                  }}
                />
              </div>
            </div>
          )}

          {/* 各镜头进度 */}
          {storyboards.map((sb) => {
            const clipResult = clipResults.find((c) => c.idx === sb.idx);
            const status = clipResult?.status || "pending";
            const statusLabels: Record<string, string> = {
              pending: "等待中",
              generating: "生成中...",
              succeeded: "已完成",
              failed: "失败",
            };
            const statusColors: Record<string, string> = {
              pending: "#9ca3af",
              generating: "#3b82f6",
              succeeded: "#10b981",
              failed: "#ef4444",
            };

            return (
              <div key={sb.idx} style={{ marginBottom: 20, padding: 16, border: "1px solid #e5e7eb", borderRadius: 8 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <span style={{ fontSize: 14, fontWeight: 600 }}>镜头 {sb.idx + 1}</span>
                    <span
                      style={{
                        padding: "4px 8px",
                        borderRadius: 4,
                        fontSize: 12,
                        fontWeight: 500,
                        background: `${statusColors[status]}20`,
                        color: statusColors[status],
                      }}
                    >
                      {statusLabels[status]}
                    </span>
                  </div>
                  {status === "succeeded" && (
                    <button
                      onClick={() => handleRegenerateClip(sb.idx)}
                      style={{
                        padding: "6px 12px",
                        borderRadius: 6,
                        border: "1px solid #d1d5db",
                        background: "white",
                        fontSize: 12,
                        cursor: "pointer",
                      }}
                    >
                      重新生成
                    </button>
                  )}
                </div>

                <p style={{ fontSize: 13, color: "#6b7280", marginBottom: 12 }}>{sb.desc}</p>

                {status === "succeeded" && clipResult?.video_url && (
                  <video
                    src={clipResult.video_url}
                    controls
                    style={{ width: "100%", borderRadius: 8, marginTop: 8 }}
                  />
                )}

                {status === "failed" && (
                  <div style={{ padding: 12, background: "#fef2f2", borderRadius: 6, color: "#dc2626", fontSize: 13 }}>
                    生成失败，请点击"重新生成"重试
                  </div>
                )}
              </div>
            );
          })}

          {/* 完成按钮 */}
          {clipResults.length === storyboards.length &&
            clipResults.every((c) => c.status === "succeeded") && (
              <div style={{ marginTop: 24, display: "flex", justifyContent: "flex-end", gap: 12 }}>
                <button
                  onClick={() => setStep("confirm")}
                  style={{
                    padding: "12px 24px",
                    borderRadius: 8,
                    border: "1px solid #d1d5db",
                    background: "white",
                    fontSize: 14,
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  返回重新生成
                </button>
                <button
                  onClick={handleStitch}
                  disabled={stitching}
                  style={{
                    padding: "12px 24px",
                    borderRadius: 8,
                    background: stitching ? "#9ca3af" : "#4f46e5",
                    color: "white",
                    border: 0,
                    fontSize: 14,
                    fontWeight: 600,
                    cursor: stitching ? "not-allowed" : "pointer",
                  }}
                >
                  {stitching ? "拼接中..." : "确认并拼接视频"}
                </button>
              </div>
            )}
        </div>
      )}

      {/* 步骤5（旧版兼容）：生成结果 */}
      {step === "generating" && clipResults.length > 0 && storyboards.length === 0 && (
        <div style={{ background: "white", padding: 32, borderRadius: 12, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 24 }}>步骤 5：镜头生成结果</h2>

          {clipResults.map((clip) => (
            <div key={clip.idx} style={{ marginBottom: 20, padding: 16, border: "1px solid #e5e7eb", borderRadius: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <span style={{ fontSize: 14, fontWeight: 600 }}>镜头 {clip.idx + 1}</span>
                <button
                  onClick={() => handleRegenerateClip(clip.idx)}
                  style={{
                    padding: "6px 12px",
                    borderRadius: 6,
                    border: "1px solid #d1d5db",
                    background: "white",
                    fontSize: 12,
                    cursor: "pointer",
                  }}
                >
                  重新生成
                </button>
              </div>
              {clip.video_url && (
                <video src={clip.video_url} controls style={{ width: "100%", borderRadius: 8 }} />
              )}
              <div style={{ fontSize: 12, color: "#6b7280", marginTop: 8 }}>状态: {clip.status}</div>
            </div>
          ))}

          {clipResults.length === storyboards.length && (
            <button
              onClick={handleStitch}
              disabled={stitching}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                background: stitching ? "#9ca3af" : "#10b981",
                color: "white",
                border: 0,
                fontSize: 14,
                fontWeight: 600,
                cursor: stitching ? "not-allowed" : "pointer",
                marginTop: 24,
              }}
            >
              {stitching ? "拼接中..." : "确认并拼接视频"}
            </button>
          )}
        </div>
      )}

      {/* 步骤6：最终结果 */}
      {step === "done" && finalVideoUrl && (
        <div style={{ background: "white", padding: 32, borderRadius: 12, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 24 }}>完成！</h2>
          <video src={finalVideoUrl} controls style={{ width: "100%", borderRadius: 8, marginBottom: 24 }} />
          <div style={{ display: "flex", gap: 12 }}>
            <button
              onClick={() => router.push(`/j/${runId}`)}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                background: "#4f46e5",
                color: "white",
                border: 0,
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              查看分享页
            </button>
            <button
              onClick={() => {
                setStep("collect");
                setSubject("");
                setStoryboards([]);
                setClipResults([]);
                setFinalVideoUrl(null);
                setRunId(null);
              }}
              style={{
                padding: "12px 24px",
                borderRadius: 8,
                border: "1px solid #d1d5db",
                background: "white",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              重新开始
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
