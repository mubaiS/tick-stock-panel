// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type StockSectorLabelsSnapshot } from '@/lib/api'
import { StockSectorLabelsContext, StockSectorLabelsProvider, StockSectorTags, sectorLabelsToday } from './StockSectorTags'
import { StockFinancialSearch } from './financials/StockFinancialSearch'
import { ScreenerTable } from './screener/ScreenerTable'
import { StockInfoBar } from './StockInfoBar'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

const snapshot: StockSectorLabelsSnapshot = {
  as_of: sectorLabelsToday(), updated_at: '2026-10-29T10:00:00+08:00',
  concept_source: 'concept.theme', industry_source: 'industry.name',
  concept_heat: { 机器人: 80 },
  stocks: { '000001.SZ': { concept: '机器人', industry: '专用设备' } },
  aliases: { '000001': '000001.SZ' },
}
const container = document.createElement('div')
document.body.append(container)
let root = createRoot(container)
afterEach(async () => { await act(async () => root.unmount()); root = createRoot(container); vi.restoreAllMocks() })

it('shares one request across list, search and dialog labels', async () => {
  const request = vi.spyOn(api, 'stockSectorLabels').mockResolvedValue(snapshot)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={client}><StockSectorLabelsProvider>
      <ul><li><StockSectorTags symbol="000001.SZ" /></li></ul>
      <button role="option"><StockSectorTags symbol="000001" /></button>
      <div role="dialog"><StockSectorTags symbol="000001.SZ" /></div>
    </StockSectorLabelsProvider></QueryClientProvider>)
  })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(request).toHaveBeenCalledTimes(1)
  for (const selector of ['li', '[role=option]', '[role=dialog]']) {
    expect(container.querySelector(selector)!.textContent).toBe('今日概念：机器人行业：专用设备')
  }
  client.clear()
})

it('does not alias an index to a stock or label ETFs', async () => {
  await act(async () => root.render(<StockSectorLabelsContext.Provider value={{ data: snapshot, day: sectorLabelsToday() }}>
    <StockSectorTags symbol="000001.SH" />
    <StockSectorTags symbol="000001.SZ" assetType="index" />
    <StockSectorTags symbol="510300.SH" assetType="etf" />
  </StockSectorLabelsContext.Provider>))
  expect(container.textContent).toBe('')
})

it('labels stale dates and retains available values on refresh failure', async () => {
  const data = { ...snapshot, as_of: '2020-01-02', stocks: { '000001.SZ': { concept: null, industry: '银行' } } }
  await act(async () => root.render(<StockSectorLabelsContext.Provider value={{ data, error: true, day: sectorLabelsToday() }}>
    <StockSectorTags symbol="000001.SZ" />
  </StockSectorLabelsContext.Provider>))
  expect(container.textContent).toBe('2020-01-02 概念：待更新行业：银行')
  expect(container.querySelector('[data-stock-sector-tags]')?.getAttribute('title')).toContain('刷新失败')
})

it('does not retain previous stock labels after switching symbols', async () => {
  const render = (symbol: string) => <StockSectorLabelsContext.Provider value={{ data: snapshot, day: sectorLabelsToday() }}><StockSectorTags symbol={symbol} assetType="stock" /></StockSectorLabelsContext.Provider>
  await act(async () => root.render(render('000001.SZ')))
  await act(async () => root.render(render('600001.SH')))
  expect(container.textContent).not.toContain('机器人')
  expect(container.textContent).not.toContain('专用设备')
})

it('renders labels in real search results, list rows and a loading stock header without changing selection', async () => {
  const rows = [{ symbol: '000001.SZ', name: '平安银行', code: '000001', asset_type: 'stock' },
    { symbol: '510300.SH', name: '沪深300ETF', code: '510300', asset_type: 'etf' }]
  vi.spyOn(api, 'instrumentSearch').mockResolvedValue({ results: rows })
  const onSelect = vi.fn()
  const onPreview = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockSectorLabelsContext.Provider value={{ data: snapshot, day: sectorLabelsToday() }}>
      <section id="search"><StockFinancialSearch onSelect={onSelect} /></section>
      <section id="header"><StockInfoBar symbol="000001.SZ" name="平安银行" rows={[]} fields={[]} onFieldsChange={() => {}} /></section>
      <ScreenerTable rows={rows.slice(0, 1)} columns={[{ id: 'symbol', label: '股票', visible: true, source: { type: 'builtin', key: 'symbol' } }]}
        strategyIdToName={{}} symbolStrategyMap={new Map()} activeStrategy={null} watchlistSet={new Set()}
        onPreview={onPreview} onAddToWatchlist={() => {}} onRemoveFromWatchlist={() => {}} watchlistPending={false} />
    </StockSectorLabelsContext.Provider>
  </QueryClientProvider>))
  const input = container.querySelector('input')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '平安')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  const results = container.querySelectorAll('#search button')
  expect(results).toHaveLength(2)
  expect(results[0].textContent).toContain('今日概念：机器人行业：专用设备')
  expect(results[1].querySelector('[data-stock-sector-tags]')).toBeNull()
  expect(container.querySelector('tbody tr')!.textContent).toContain('今日概念：机器人行业：专用设备')
  expect(container.querySelector('#header')!.textContent).toContain('今日概念：机器人行业：专用设备')
  await act(async () => (results[0] as HTMLButtonElement).click())
  expect(onSelect).toHaveBeenCalledWith('000001.SZ', '平安银行')
  await act(async () => container.querySelector<HTMLButtonElement>('tbody tr button')!.click())
  expect(onPreview).toHaveBeenCalledWith('000001.SZ', '平安银行', [{ symbol: '000001.SZ', name: '平安银行' }])
  client.clear()
})

it('refreshes the shared snapshot when the mapping source changes', async () => {
  const request = vi.spyOn(api, 'stockSectorLabels').mockResolvedValue(snapshot)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<QueryClientProvider client={client}><StockSectorLabelsProvider>
    <StockSectorTags symbol="000001.SZ" />
  </StockSectorLabelsProvider></QueryClientProvider>))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  request.mockResolvedValue({ ...snapshot, stocks: { '000001.SZ': { concept: '新概念', industry: '银行' } } })
  await act(async () => { client.setQueryData(['preferences'], { monitor_ext_fields: {} }) })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(request).toHaveBeenCalledTimes(2)
  expect(container.textContent).toBe('今日概念：新概念行业：银行')
  client.clear()
})
