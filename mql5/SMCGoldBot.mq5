//+------------------------------------------------------------------+
//|                                                   SMCGoldBot.mq5 |
//|  MQL5 port of the Python SMC gold bot (strategy_smc.py + bot.py)  |
//|  for MT5's Strategy Tester and MT5 VPS hosting.                   |
//|                                                                  |
//|  Logic (same as Python):                                         |
//|   HTF (H1): causal BOS/CHoCH by body close; demand/supply OB POI |
//|             at every break; POI dies when closed through / old /  |
//|             trend flips.                                         |
//|   Trend filter: H1 close vs EMA200 and EMA slope over 10 bars.   |
//|   LTF (M15): price trades into aligned POI, sweeps an LTF swing, |
//|             then first close beyond the latest LTF swing (CHoCH).|
//|   Exits: TP at 3R, SL to breakeven at 1R, close on HTF flip.      |
//|  Decisions are made once per closed LTF bar, like the backtest.  |
//+------------------------------------------------------------------+
#property copyright "gold-bot"
#property version   "1.00"

#include <Trade\Trade.mqh>

//--- risk
input double InpRiskPercent      = 2.0;    // Risk per trade (% of equity)
input int    InpMaxSpreadPoints  = 50;     // Skip entries if spread above (points)
input double InpMaxDailyDDPct    = 6.0;    // No new trades after this daily loss (%)
input long   InpMagic            = 7770001;
input int    InpDeviation        = 10;     // Max slippage (points)
//--- timeframes
input ENUM_TIMEFRAMES InpHTF     = PERIOD_H1;   // Structure timeframe
input ENUM_TIMEFRAMES InpLTF     = PERIOD_M15;  // Entry timeframe
input int    InpHTFBars          = 2000;   // HTF bars used for the structure pass
//--- kill zones (UTC hours, inclusive like Python: start <= t <= end)
input int    InpLondonStart      = 6;
input int    InpLondonEnd        = 12;
input int    InpNYStart          = 12;
input int    InpNYEnd            = 20;
input int    InpServerUTCWinter  = 2;      // Server UTC offset in winter (MetaQuotes: 2)
input bool   InpServerEUDST      = true;   // Server follows EU daylight saving (+1 in summer)
//--- HTF structure
input int    InpSwingLookback    = 5;
input int    InpMinBos           = 1;
input int    InpPoiMaxAge        = 192;    // HTF bars
input int    InpObSearch         = 5;
//--- trend filter
input bool   InpTrendFilter      = true;
input ENUM_TIMEFRAMES InpTrendTF = PERIOD_H1;
input int    InpTrendEMA         = 200;
input int    InpTrendSlopeBars   = 10;
//--- LTF trigger
input int    InpLtfSwingLen      = 2;
input int    InpSweepLookback    = 20;
input int    InpSweepRefBars     = 30;
input bool   InpSweepAny         = true;
input int    InpChochLookback    = 15;
input bool   InpRequireSweep     = true;
input bool   InpSLModePoi        = false;  // false: SL beyond sweep wick; true: beyond sweep or POI
input double InpMinSLAtr         = 0.5;
input int    InpAtrPeriod        = 14;
//--- exits
input bool   InpExitTrail        = false;  // false: fixed TP (rr) | true: ATR trailing stop
input double InpTP_R             = 3.0;
input double InpBE_R             = 1.0;    // 0 = off
input double InpTrailMult        = 1.5;    // x HTF ATR (trail mode)
input bool   InpExitOnBiasFlip   = true;

//+------------------------------------------------------------------+
struct POI
  {
   int      dir;       // +1 demand, -1 supply
   double   top;
   double   bottom;
   int      created;   // index in the HTF window
   int      dead;      // index when it died (n = still alive)
   datetime ctime;     // time of the HTF bar that created it (stable id)
  };

CTrade   trade;
POI      g_pois[];          // alive POIs after the last closed HTF bar (creation order)
int      g_bias      = 0;   // +1 / -1 / 0
double   g_htfAtr    = 0;
datetime g_lastHTF   = 0;
datetime g_lastLTF   = 0;
string   g_used[];          // POI ids already traded
int      g_dayKey    = -1;
double   g_dayStartEq= 0;
double   g_extreme   = 0;   // best price since entry (BE / trail), from closed bars
ulong    g_posTicket = 0;
bool     g_beDone    = false;
int      g_emaHandle = INVALID_HANDLE;

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpDeviation);
   trade.SetTypeFillingBySymbol(_Symbol);
   if(InpTrendFilter)
     {
      g_emaHandle = iMA(_Symbol, InpTrendTF, InpTrendEMA, 0, MODE_EMA, PRICE_CLOSE);
      if(g_emaHandle == INVALID_HANDLE)
        { Print("Failed to create EMA handle"); return INIT_FAILED; }
     }
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   if(g_emaHandle != INVALID_HANDLE) IndicatorRelease(g_emaHandle);
  }

//+------------------------------------------------------------------+
//| Time helpers: server time -> UTC (EET with EU DST by default)    |
//+------------------------------------------------------------------+
datetime LastSundayUTC(int year, int month)   // last Sunday of month, 01:00 UTC
  {
   MqlDateTime d; d.year=year; d.mon=month+1; d.day=1; d.hour=0; d.min=0; d.sec=0;
   if(month == 12) { d.year = year+1; d.mon = 1; }
   datetime firstNext = StructToTime(d);
   datetime last = firstNext - 86400;
   MqlDateTime l; TimeToStruct(last, l);
   last -= l.day_of_week * 86400;            // back to Sunday
   return last + 3600;                       // EU switch happens at 01:00 UTC
  }

datetime ServerToUTC(datetime server)
  {
   datetime utc = server - InpServerUTCWinter*3600;
   if(InpServerEUDST)
     {
      MqlDateTime s; TimeToStruct(utc, s);
      datetime start = LastSundayUTC(s.year, 3);
      datetime end   = LastSundayUTC(s.year, 10);
      datetime utcSummer = server - (InpServerUTCWinter+1)*3600;
      if(utcSummer >= start && utcSummer < end) utc = utcSummer;
     }
   return utc;
  }

bool InKillZone(datetime server)
  {
   MqlDateTime u; TimeToStruct(ServerToUTC(server), u);
   int mins = u.hour*60 + u.min;
   bool london = mins >= InpLondonStart*60 && mins <= InpLondonEnd*60;
   bool ny     = mins >= InpNYStart*60     && mins <= InpNYEnd*60;
   return london || ny;
  }

//+------------------------------------------------------------------+
//| Indicators on plain arrays (index 0 = oldest)                    |
//+------------------------------------------------------------------+
double AtrAt(const MqlRates &r[], int t, int period)   // SMA of true range, like pandas version
  {
   if(t < period) return 0;
   double sum = 0;
   for(int i = t-period+1; i <= t; i++)
     {
      double pc = r[i-1].close;
      double tr = MathMax(r[i].high - r[i].low, MathMax(MathAbs(r[i].high - pc), MathAbs(r[i].low - pc)));
      sum += tr;
     }
   return sum / period;
  }

void SwingFlags(const double &h[], const double &l[], int n, int L, bool &sh[], bool &sl[])
  {
   ArrayResize(sh, n); ArrayResize(sl, n);
   ArrayInitialize(sh, false); ArrayInitialize(sl, false);
   for(int j = L; j < n-L; j++)
     {
      double mx = h[j], mn = l[j];
      for(int k = j-L; k <= j+L; k++) { mx = MathMax(mx, h[k]); mn = MathMin(mn, l[k]); }
      sh[j] = (h[j] >= mx);
      sl[j] = (l[j] <= mn);
     }
  }

//+------------------------------------------------------------------+
//| HTF structure pass (port of SMCStrategy.htf_structure)           |
//+------------------------------------------------------------------+
void AddPoi(POI &act[], int dir, double top, double bottom, int t, datetime ctime, int n)
  {
   if(top <= bottom) return;
   int k = ArraySize(act); ArrayResize(act, k+1);
   act[k].dir=dir; act[k].top=top; act[k].bottom=bottom; act[k].created=t; act[k].dead=n; act[k].ctime=ctime;
  }

void RemoveAt(POI &a[], int i)
  {
   int n = ArraySize(a);
   for(int k = i; k < n-1; k++) a[k] = a[k+1];
   ArrayResize(a, n-1);
  }

bool UpdateHTF()
  {
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int n = CopyRates(_Symbol, InpHTF, 1, InpHTFBars, r);     // closed bars, oldest first
   if(n < 4*InpSwingLookback) return false;

   double o[], h[], l[], c[];
   ArrayResize(o,n); ArrayResize(h,n); ArrayResize(l,n); ArrayResize(c,n);
   for(int i=0;i<n;i++){ o[i]=r[i].open; h[i]=r[i].high; l[i]=r[i].low; c[i]=r[i].close; }
   bool ish[], isl[];
   int L = InpSwingLookback;
   SwingFlags(h, l, n, L, ish, isl);

   POI act[]; ArrayResize(act, 0);
   int trend = 0, bos = 0;
   double shP=0, slP=0; int shI=-1, slI=-1;
   bool shBroken=true, slBroken=true;

   for(int t = 0; t < n; t++)
     {
      int j = t - L;
      if(j >= L)
        {
         if(ish[j]) { shP=h[j]; shI=j; shBroken=false; }
         if(isl[j]) { slP=l[j]; slI=j; slBroken=false; }
        }
      // kill closed-through / old POIs
      for(int i = ArraySize(act)-1; i >= 0; i--)
        {
         bool broken = act[i].dir > 0 ? (c[t] < act[i].bottom) : (c[t] > act[i].top);
         if(broken || t - act[i].created > InpPoiMaxAge) RemoveAt(act, i);
        }
      // bullish break
      if(shI >= 0 && !shBroken && c[t] > shP)
        {
         shBroken = true;
         bos = (trend == 1) ? bos+1 : 1;
         if(trend != 1)
            for(int i = ArraySize(act)-1; i >= 0; i--) if(act[i].dir < 0) RemoveAt(act, i);
         trend = 1;
         int k = shI;
         for(int m = shI; m <= t; m++) if(l[m] < l[k]) k = m;          // leg origin (first lowest low)
         int ob = k;
         for(int m = k; m > MathMax(k - InpObSearch, -1); m--) if(c[m] < o[m]) { ob = m; break; }
         bool closedThrough = false;
         for(int m = ob+1; m <= t; m++) if(c[m] < l[ob]) { closedThrough = true; break; }
         if(!closedThrough) AddPoi(act, +1, h[ob], l[ob], t, r[t].time, n);
        }
      else if(slI >= 0 && !slBroken && c[t] < slP)
        {
         slBroken = true;
         bos = (trend == -1) ? bos+1 : 1;
         if(trend != -1)
            for(int i = ArraySize(act)-1; i >= 0; i--) if(act[i].dir > 0) RemoveAt(act, i);
         trend = -1;
         int k = slI;
         for(int m = slI; m <= t; m++) if(h[m] > h[k]) k = m;          // first highest high
         int ob = k;
         for(int m = k; m > MathMax(k - InpObSearch, -1); m--) if(c[m] > o[m]) { ob = m; break; }
         bool closedThrough = false;
         for(int m = ob+1; m <= t; m++) if(c[m] > h[ob]) { closedThrough = true; break; }
         if(!closedThrough) AddPoi(act, -1, h[ob], l[ob], t, r[t].time, n);
        }
     }
   g_bias = (bos >= InpMinBos) ? trend : 0;
   ArrayResize(g_pois, ArraySize(act));
   for(int i=0;i<ArraySize(act);i++) g_pois[i] = act[i];
   g_htfAtr = AtrAt(r, n-1, InpAtrPeriod);
   return true;
  }

//+------------------------------------------------------------------+
int TrendDirection()
  {
   if(!InpTrendFilter) return g_bias;
   double ema[];
   ArraySetAsSeries(ema, true);
   if(CopyBuffer(g_emaHandle, 0, 1, InpTrendSlopeBars+1, ema) < InpTrendSlopeBars+1) return 0;
   double close = iClose(_Symbol, InpTrendTF, 1);
   double slope = ema[0] - ema[InpTrendSlopeBars];
   if(close > ema[0] && slope > 0) return 1;
   if(close < ema[0] && slope < 0) return -1;
   return 0;
  }

string PoiId(const POI &p) { return StringFormat("OB%+d@%I64d", p.dir, (long)p.ctime); }

bool IsUsed(string id)
  {
   for(int i=0;i<ArraySize(g_used);i++) if(g_used[i]==id) return true;
   return false;
  }

//+------------------------------------------------------------------+
//| LTF trigger (port of SMCStrategy.ltf_signal)                     |
//+------------------------------------------------------------------+
int LtfSignal(int bias, double spreadPrice, double &slOut, int &poiIdx)
  {
   if(bias == 0) return 0;
   int Ls = InpLtfSwingLen, N = InpSweepLookback, ref = InpSweepRefBars;
   int win = N + ref + 2*Ls + 1;
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int got = CopyRates(_Symbol, InpLTF, 1, win + InpAtrPeriod + 1, r);   // closed bars, oldest first
   if(got < win + InpAtrPeriod + 1) return 0;
   int off = got - win;                       // window = last `win` bars
   double hh[], ll[], cc[], oo[];
   ArrayResize(hh,win); ArrayResize(ll,win); ArrayResize(cc,win); ArrayResize(oo,win);
   for(int i=0;i<win;i++){ hh[i]=r[off+i].high; ll[i]=r[off+i].low; cc[i]=r[off+i].close; oo[i]=r[off+i].open; }
   int T = win-1;
   bool ish[], isl[];
   SwingFlags(hh, ll, win, Ls, ish, isl);
   for(int i = T-Ls+1; i <= T; i++) { ish[i]=false; isl[i]=false; }

   // sweep candle = extreme of the last N bars
   int m = T-N+1;
   for(int i = T-N+1; i <= T; i++)
     {
      if(bias > 0 && ll[i] < ll[m]) m = i;
      if(bias < 0 && hh[i] > hh[m]) m = i;
     }
   double extreme = bias > 0 ? ll[m] : hh[m];

   poiIdx = -1;
   for(int i=0;i<ArraySize(g_pois);i++)
     {
      if(g_pois[i].dir != bias || IsUsed(PoiId(g_pois[i]))) continue;
      double sz = g_pois[i].top - g_pois[i].bottom;
      bool inside = bias > 0 ? (g_pois[i].bottom - sz <= extreme && extreme <= g_pois[i].top)
                             : (g_pois[i].bottom <= extreme && extreme <= g_pois[i].top + sz);
      if(inside) { poiIdx = i; break; }
     }
   if(poiIdx < 0) return 0;

   if(InpRequireSweep)
     {
      int lo = MathMax(0, m-ref);
      bool any=false; double best = 0, last = 0;
      for(int i = lo; i < m; i++)
        {
         if(bias > 0 && isl[i]) { if(!any || ll[i] > best) best = ll[i]; last = ll[i]; any = true; }
         if(bias < 0 && ish[i]) { if(!any || hh[i] < best) best = hh[i]; last = hh[i]; any = true; }
        }
      if(!any) return 0;
      double lvl = InpSweepAny ? best : last;
      if(bias > 0 && !(extreme < lvl)) return 0;
      if(bias < 0 && !(extreme > lvl)) return 0;
     }

   // CHoCH: this bar is the first close beyond the latest LTF swing
   int from = MathMax(0, m - InpChochLookback);
   int k = -1;
   for(int i = from; i < T; i++) { if(bias > 0 && ish[i]) k = i; if(bias < 0 && isl[i]) k = i; }
   if(k < 0) return 0;
   double level = bias > 0 ? hh[k] : ll[k];
   if(bias > 0)
     {
      if(!(cc[T] > level)) return 0;
      for(int i = k+1; i < T; i++) if(cc[i] > level) return 0;
     }
   else
     {
      if(!(cc[T] < level)) return 0;
      for(int i = k+1; i < T; i++) if(cc[i] < level) return 0;
     }

   double atr = AtrAt(r, got-1, InpAtrPeriod);
   double buffer = MathMax(2*spreadPrice, 0.1*atr);
   double sl;
   if(InpSLModePoi)
      sl = bias > 0 ? MathMin(extreme, g_pois[poiIdx].bottom) - buffer : MathMax(extreme, g_pois[poiIdx].top) + buffer;
   else
      sl = bias > 0 ? extreme - buffer : extreme + buffer;
   double dist = MathAbs(cc[T] - sl);
   if(dist < InpMinSLAtr*atr || (spreadPrice > 0 && spreadPrice > 0.25*dist)) return 0;
   slOut = sl;
   return bias;
  }

//+------------------------------------------------------------------+
//| Position helpers                                                 |
//+------------------------------------------------------------------+
bool GetMyPosition(ulong &ticket, int &dir, double &openPrice, double &sl, double &tp)
  {
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      ticket = tk;
      dir = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      sl = PositionGetDouble(POSITION_SL);
      tp = PositionGetDouble(POSITION_TP);
      return true;
     }
   return false;
  }

double LotsForRisk(int dir, double entry, double sl)
  {
   double loss;
   ENUM_ORDER_TYPE type = dir > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcProfit(type, _Symbol, 1.0, entry, sl, loss) || loss >= 0) return 0;
   loss = -loss;
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lots = MathFloor(equity * InpRiskPercent / 100.0 / loss / step + 1e-9) * step;
   lots = MathMin(lots, vmax);
   if(lots < vmin) return 0;
   return NormalizeDouble(lots, 2);
  }

bool DailyOK(datetime server)
  {
   MqlDateTime u; TimeToStruct(ServerToUTC(server), u);
   int key = u.year*1000 + u.day_of_year;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(key != g_dayKey) { g_dayKey = key; g_dayStartEq = eq; }
   return (eq - g_dayStartEq) / g_dayStartEq * 100.0 > -InpMaxDailyDDPct;
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   datetime ltfTime = iTime(_Symbol, InpLTF, 0);
   if(ltfTime == 0 || ltfTime == g_lastLTF) return;     // act once per new LTF bar
   g_lastLTF = ltfTime;

   // 1. HTF state after the last closed HTF bar
   datetime htfTime = iTime(_Symbol, InpHTF, 0);
   if(htfTime != g_lastHTF)
     {
      if(!UpdateHTF()) return;
      g_lastHTF = htfTime;
     }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   double spread = tick.ask - tick.bid;

   // 2. manage the open position (using the bar that just closed)
   ulong tk; int dir; double op, sl, tp;
   if(GetMyPosition(tk, dir, op, sl, tp))
     {
      if(tk != g_posTicket) { g_posTicket = tk; g_extreme = op; g_beDone = false; }
      double hi = iHigh(_Symbol, InpLTF, 1), lo = iLow(_Symbol, InpLTF, 1);
      g_extreme = dir > 0 ? MathMax(g_extreme, hi) : MathMin(g_extreme, lo + spread);
      double fav = (g_extreme - op) * dir;
      double r = (tp > 0) ? MathAbs(tp - op) / InpTP_R : 0;
      double newSL = sl;
      if(!InpExitTrail && InpBE_R > 0 && !g_beDone && r > 0 && fav >= InpBE_R * r)
        {
         newSL = dir > 0 ? MathMax(sl, op) : (sl == 0 ? op : MathMin(sl, op));
         g_beDone = true;
        }
      if(InpExitTrail && fav > 0 && g_htfAtr > 0)
        {
         double d = InpTrailMult * g_htfAtr;
         newSL = dir > 0 ? MathMax(newSL, g_extreme - d) : MathMin(newSL == 0 ? DBL_MAX : newSL, g_extreme + d);
        }
      if(MathAbs(newSL - sl) > _Point/2)
         trade.PositionModify(tk, NormalizeDouble(newSL, _Digits), tp);
      if(InpExitOnBiasFlip && g_bias == -dir)
        {
         trade.PositionClose(tk);
         Print("Bias flipped against position - closed");
        }
      return;                                            // one position at a time
     }

   // 3. entries: kill zone, daily limit, spread, trend filter
   if(!InKillZone(TimeCurrent())) return;
   if(!DailyOK(TimeCurrent())) return;
   if(spread / _Point > InpMaxSpreadPoints) return;
   int b = g_bias;
   if(InpTrendFilter && TrendDirection() != b) b = 0;

   double slPrice; int pi;
   int sig = LtfSignal(b, spread, slPrice, pi);
   if(sig == 0) return;

   double entry = sig > 0 ? tick.ask : tick.bid;
   double lots = LotsForRisk(sig, entry, slPrice);
   if(lots <= 0) { Print("Lot size below minimum for risk budget - skipped"); return; }
   double tpPrice = InpExitTrail ? 0 : NormalizeDouble(entry + sig * InpTP_R * MathAbs(entry - slPrice), _Digits);
   slPrice = NormalizeDouble(slPrice, _Digits);
   bool ok = sig > 0 ? trade.Buy(lots, _Symbol, 0, slPrice, tpPrice, "SMC Gold Bot")
                     : trade.Sell(lots, _Symbol, 0, slPrice, tpPrice, "SMC Gold Bot");
   if(ok && trade.ResultRetcode() == TRADE_RETCODE_DONE)
     {
      int k = ArraySize(g_used); ArrayResize(g_used, k+1); g_used[k] = PoiId(g_pois[pi]);
      PrintFormat("%s %.2f lots | POI %.2f-%.2f | SL %.2f TP %.2f", sig>0?"BUY":"SELL", lots,
                  g_pois[pi].bottom, g_pois[pi].top, slPrice, tpPrice);
     }
   else
      PrintFormat("Order failed: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
  }
//+------------------------------------------------------------------+
