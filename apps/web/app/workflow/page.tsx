"use client";
import { useState, Suspense } from "react";
import { useRouter } from "next/navigation";
import { WorkflowContent } from "./WorkflowContent";

export default function WorkflowPage() {
  return (
    <Suspense fallback={
      <div style={{ padding: 40, textAlign: "center", color: "#9ca3af" }}>
        加载中...
      </div>
    }>
      <WorkflowContent />
    </Suspense>
  );
}

