"use client";

import { Component, ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: any) {
    console.error("ErrorBoundary caught an error:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      return (
        <div style={{ padding: 24, textAlign: "center" }}>
          <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 12 }}>
            出现了一些问题
          </h2>
          <p style={{ color: "#6b7280", marginBottom: 16 }}>
            {this.state.error?.message || "未知错误"}
          </p>
          <button
            onClick={() => {
              this.setState({ hasError: false, error: undefined });
              window.location.reload();
            }}
            style={{
              padding: "8px 16px",
              border: "1px solid #ddd",
              borderRadius: 6,
              background: "white",
              cursor: "pointer",
            }}
          >
            重新加载
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

