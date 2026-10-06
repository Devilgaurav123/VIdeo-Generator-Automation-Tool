# Google Flow integration boundary

Google Flow UI/Agent and the Veo/Gemini API are separate systems. SpectreAI never labels a Flow screen action as an official API call.

When a permitted integration is later added, enable `FLOW_PROVIDER_ENABLED=true` and provide an authenticated browser profile outside source control. The adapter must preserve project context, Agent Instructions, character references, style bible, and sequential shot prompts. It must stop on CAPTCHA, quota, payment, credit, or session failure. It must never rotate accounts, purchase credits, upgrade plans, or remove provider branding.

When using a supported Veo/Gemini API instead, enable `VEO_API_ENABLED=true` and implement the same `VideoProvider` interface. The production pipeline, database state machine, credit gate, retries, and QC must remain unchanged.