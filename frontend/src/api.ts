import Constants from "expo-constants";
import { storage } from "@/src/utils/storage";

export type Project = { id: string; prompt: string; target_duration_minutes: number; visual_style: string; aspect_ratio: string; language: string; voice_style: string; created_at: string; latest_job_id?: string | null };
export type Job = { id: string; project_id: string; status: string; stage: string; progress: number; current_shot: number; total_shots: number; message: string; created_at: string; updated_at: string; story?: { title: string; premise: string; narration: string; target_duration_seconds: number }; characters?: { id: string; name: string; role: string; clothing: string; continuity_rules: string }[]; scenes?: { id: string; number: number; title: string; mood: string }[]; clips?: { id: string; shot_id: string; status: string; duration_seconds: number }[]; audio?: { id: string; type: string; status: string; duration_seconds: number }[]; style_bible?: { animation_style: string; palette: string[]; atmosphere: string; aspect_ratio: string }; quality_report?: { passed: boolean; checks: string[]; warnings: string[] }; credit_status?: { allowed: boolean; reason: string; provider: string }; final_asset?: { filename: string; download_ready: boolean; note?: string } };

const baseUrl = String(Constants.expoConfig?.extra?.backendUrl ?? process.env.EXPO_BACKEND_URL ?? process.env.EXPO_PUBLIC_BACKEND_URL ?? "").replace(/\/$/, "");
const tokenKey = "spectreai_token";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = await storage.secureGet(tokenKey, null);
  const response = await fetch(`${baseUrl}${path}`, { ...options, headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(options.headers ?? {}) } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.detail?.message ?? body?.detail ?? "Something went wrong");
  return body as T;
}

export async function authenticate(email: string, password: string, mode: "login" | "register") {
  const result = await request<{ access_token: string; user: { id: string; email: string } }>(`/api/v1/auth/${mode}`, { method: "POST", body: JSON.stringify({ email, password }) });
  await storage.secureSet(tokenKey, result.access_token);
  return result.user;
}

export async function getMe() { return request<{ id: string; email: string }>("/api/v1/auth/me"); }
export async function signOut() { await storage.secureRemove(tokenKey); }
export async function listProjects() { return request<Project[]>("/api/v1/projects"); }
export async function createProject(input: Omit<Project, "id" | "created_at" | "latest_job_id">) { return request<Project>("/api/v1/projects", { method: "POST", body: JSON.stringify(input) }); }
export async function startGeneration(projectId: string) { return request<{ job_id: string; status: string }>(`/api/v1/projects/${projectId}/generate`, { method: "POST" }); }
export async function getJob(jobId: string) { return request<Job>(`/api/v1/jobs/${jobId}`); }
export async function cancelJob(jobId: string) { return request<{ status: string }>(`/api/v1/jobs/${jobId}/cancel`, { method: "POST", body: JSON.stringify({}) }); }
export async function resumeJob(jobId: string) { return request<{ status: string }>(`/api/v1/jobs/${jobId}/resume`, { method: "POST" }); }