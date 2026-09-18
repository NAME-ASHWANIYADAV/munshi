# MunshiJi — Finale Runbook (Sep 19, Paytm Noida)

Reporting 9:00 AM · gates close 10:00 AM. Sab kuch is ek page pe hai.
Phone A = merchant (MunshiJi APK + WhatsApp, +91 9310036701). Phone B = teammate = "customer".
Laptop = n8n canvases on projector.

---

## GHAR SE NIKALNE SE PEHLE (7:15–8:00)

**1. Meta token refresh (token 24h ka hai — ye sabse zaroori step hai):**
- developers.facebook.com → My Apps → MunshiJi Demo → WhatsApp use case → Step 1. Try it out
- **Generate access token** → copy
- n8n → **Variables** → `WHATSAPP_TOKEN` → nayi value → Save
- n8n → **Credentials** → "WhatsApp account" → naya Access Token → Save (green "tested successfully" dekho)

**2. 24h window kholo:** DONO phones se test number **+1 (555) 170-5247** ko WhatsApp pe "hi".

**3. Phone A taiyaari:**
- MunshiJi app login (हिंदी) — mic permission **Allowed** dikhna chahiye (Settings → Apps → MunshiJi → Permissions)
- Screen timeout **10 minute** (Settings → Display) — SSE beat ke liye screen zinda chahiye
- WhatsApp notifications ON (buzz hi beat hai), baaki apps silent
- Charge 100%, ek powerbank saath

**4. Laptop taiyaari:**
- Chrome tabs pinned, is order mein: ① Shaam ka Hisaab canvas ② Outbound Actions canvas ③ n8n Executions list ④ Evaluations tab ⑤ ek PURANI GREEN execution (backup dikhane ko)
- Hotspot **Phone B ya teesre phone se** — presenting laptop se kabhi nahi. Phone A usi hotspot pe.

**5. Health check (laptop PowerShell):**
```powershell
Invoke-RestMethod https://munshiji-api-pr8v.onrender.com/api/health | Select -Expand sponsors
```
Chahiye: `cognee: live, n8n: live`. (Heartbeat workflow Render ko raat bhar garam rakhta hai.)

**6. Pending action stage karo** (Render raat mein restart hua ho toh bhi ye do lines sab theek kar deti hain):
```powershell
$b='https://munshiji-api-pr8v.onrender.com/api/chat'
$r1=Invoke-RestMethod -Method Post -Uri $b -ContentType 'application/json' -Body '{"merchant_id":"default","text":"kaun kaun purane customer nahi aa rahe?","language":"hi-IN"}'
Invoke-RestMethod -Method Post -Uri $b -ContentType 'application/json' -Body ('{"merchant_id":"default","text":"unhe 10% ka offer bhej do","language":"hi-IN","conversation_id":"'+$r1.conversation_id+'"}') | Select reply
```
Reply mein "10 ग्राहकों के लिए ऑफर तैयार... भेज दूं?" aana chahiye. **Isके baad app se approve MAT karna** — ye stage ke liye hai.

**⛔ VENUE PE KABHI NAHI:** reseed, Render manual deploy, n8n workflows edit, digest ka dry-run (action consume ho jata hai).

---

## DEMO SCRIPT (3 minute, beat-by-beat)

**0:00–0:20 · Cold open (A, phone haath mein)**
Login (हिंदी) → Dukaan. Bolo: *"Ye MunshiJi hai — Ramesh ji ka AI munim. Data ek seeded merchant ka hai — 180 din, 220 customers, statistically realistic kirana. Koi slide nahi, sab live."*

**0:20–1:00 · Voice (A)**
Mic dabao: **"आज का धंधा कैसा रहा?"** → bolta hua jawab + hero numbers.
Phir: **"किस-किस से कितना उधार बाकी है?"** → Khata tab kholo — aging bar, naam, ₹90,550.
Architecture line: *"Har capability ka live vendor bhi hai, offline twin bhi — auto mode girta hai toh bhi demo nahi girta. Sarvam ki key lagte hi voice aur reasoning wahi ho jayegi, bina code badle."*

**1:00–1:50 · n8n BEAT (B laptop pe, projector)**
B fire karta hai:
```powershell
Invoke-RestMethod -Method Post -Uri 'https://ashwaniyaduvanshi.app.n8n.cloud/webhook/munshiji-digest' -Headers @{'X-Munshiji-Token'='munshiji-KxM8ohT0INDq8Kdx9VWSDHTX'} -ContentType 'application/json' -Body '{}'
```
→ Canvas ① node-by-node jalta hai, **WhatsApp node "Waiting" pe rukta hai**
→ Phone A buzz: poora din ka hisaab + *"Send a 10% off win-back offer to 10 customers — Send it?"*
→ A **हाँ भेज दो** tap karta hai
→ Canvas ① resume → API approve → **Canvas ② (Outbound) jalta hai** → Phone A pe **10 messages** `[naam ko]` ke saath → app pe pending count live girta hai
B ki line: *"Merchant ne apni dukaan WhatsApp se chala di. Poore campaign ka kharch ₹7.80 — kyunki win-back marketing message 78 paise ka hai, udhaar reminder utility sirf 12 paise — 6 guna sasta. MunshiJi ye farak jaanta hai aur break-even bol ke deta hai."*
(Sandbox line agar poochhe: *"Demo sandbox mein saari deliveries demo phone pe route hoti hain — production mein har customer ke number pe."*)

**1:50–2:30 · Cognee BEAT (A)**
Voice ya type: **"पिछले ऑफर से कौन वापस आया?"**
→ Jawab naam-ba-naam: *"Naveen Dua, Manpreet Mishra, Jaspreet Gupta और Vijay Joshi..."*
→ Reply ke neeche **"याद से"** chip tap → Yaad page, **"Cognee graph · live"** badge, graph.
B ki line: *"Ye jawab kisi ek record mein exist nahi karta — Cognee ne knowledge graph walk kiya: offer → customer → wapsi ka din. Aur hamara doosra n8n workflow har 6 ghante ye graph khud taaza karta hai — add, cognify, search-verify."* (Do sponsor ek saans mein.)

**2:30–3:00 · Close (A)**
Dukaan pe wapas, repeat/new split dikhao: *"78 paise ka win-back, ek customer lauta toh barabar. Soundbox paisa aane ki awaaz hai — MunshiJi agla paisa lane ka haath. Aur iski voice-khata wo cash aur udhaar economy pakadti hai jo QR code tak kabhi pahunchti hi nahi. Wahi data hai jo Paytm ke paas nahi hai."*

---

## FALLBACK DRILL (jo bhi mare, agla kadam ready)

| Mare toh | Karo ye | Bolo ye |
|---|---|---|
| Mic/STT 2 baar galat | Wahi sawaal TYPE karo | "Type se bhi wahi engine, wahi trace" |
| WhatsApp/Meta | Dukaan ke sujhav card pe **हाँ भेज दो** in-app + pinned green execution tab | "Approval gate wahi hai, channel do hain" |
| Render cold/slow | App fixtures pe khud latch ho jata hai (sheet mein "demo data") | "Wifi mara, product nahi — yahi dual-provider architecture hai" |
| Cognee slow | Kuch mat karo — auto local-mirror fallback hai, jawab wahi | (kuch bolne ki zaroorat hi nahi) |
| Canvas load na ho | Executions list tab | — |

## Q&A CARD (dono raat ko ek baar bol ke practice karo)

1. **"Wrapper hai?"** → "LLM nikal do toh bhi poora product chalta hai — 672 tests, offline, 45 second. LLM ise behtar banata hai, define nahi karta."
2. **"Unit economics?"** → "Marketing 78p, utility 12p per message. ₹29,785 ka udhaar chase = 96 paise. Har proposal apna kharch aur break-even bolta hai — wahi shopkeeper ka decision format hai."
3. **"Paytm kyun?"** → "Paytm dashboard nahi bechta — payments, devices, credit distribution. MunshiJi use karta hua merchant roz wahi evidence banata hai jo bina audited accounts wale shop ko lend karne ke liye chahiye. Hum extend karte hain, compete nahi."
4. **"Data real hai?"** → "Seeded aur disclosed — pehli line mein bola tha. Production mein: Paytm rails + voice-khata khud data source hai."
5. **"Spam/consent?"** → "Abhi stage pe dekha: 15 dormant mile, 5 bina consent kat gaye, 10 gaye. DPDP ke hisaab se purchase history marketing list nahi hoti. Reminders sirf 8am–7pm (RBI recovery conduct). Tone ceiling FIRM — forbidden-terms test ke saath."

**KABHI MAT BOLNA:** "credit score/underwriting decision" (sirf "signal") · "GPT/LLM likhta hai" · Soundbox ya Business app ke against kuch · koi traction number jo exist nahi karta.

---

## Numbers jo har jawab mein kaam aayenge

- Aaj: **₹18,650** collected · 59 txns · 42 customers · projected **₹29,451**
- Udhaar: **₹90,550** · 34 khaate · 7 over-60d
- Win-back: 15 mile → **5 consent-cut** → 10 gaye → kharch **₹7.80** → wapsi **~₹1,912** (4 customers)
- Suite: **672 tests**, offline, no keys
- Stack: Sarvam (voice+LLM, local twin) · Cognee (graph memory, LIVE) · n8n (5 workflows, LIVE)
