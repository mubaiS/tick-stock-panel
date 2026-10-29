import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type StockSectorLabelsSnapshot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function sectorLabelsToday() {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
}

interface State {
  data?: StockSectorLabelsSnapshot
  error?: boolean
  day: string
}

export const StockSectorLabelsContext = createContext<State | null>(null)

/** 唯一查询/定时器挂在应用布局；数百个标签只是读取同一快照，不逐行请求。 */
export function StockSectorLabelsProvider({ children }: { children: ReactNode }) {
  const [day, setDay] = useState(sectorLabelsToday)
  const client = useQueryClient()
  const query = useQuery({
    queryKey: QK.stockSectorLabels(day),
    queryFn: () => api.stockSectorLabels(),
    staleTime: 30_000,
    refetchInterval: 30_000,
  })
  useEffect(() => {
    const timer = setInterval(() => setDay(sectorLabelsToday()), 30_000)
    // 扩展表写入/删除后，各入口都会失效或刷新 ext-data 查询。
    const unsubscribe = client.getQueryCache().subscribe(event => {
      if (event.type !== 'updated' || !['ext-data', 'ext-data-rows', 'preferences'].includes(String(event.query.queryKey[0]))) return
      if (event.action.type === 'invalidate' || event.action.type === 'success') {
        void client.invalidateQueries({ queryKey: ['stock-sector-labels'] })
      }
    })
    return () => { clearInterval(timer); unsubscribe() }
  }, [client])
  return <StockSectorLabelsContext.Provider value={{ data: query.data, error: query.isError, day }}>{children}</StockSectorLabelsContext.Provider>
}

interface Props {
  symbol?: string | null
  assetType?: string | null
  className?: string
  onTagClick?: (kind: 'concept' | 'industry', value: string, source: string) => void
}

/** 全站只展示一个最热概念；日期常显，历史页面也不会把当前标签误当历史归属。 */
export function StockSectorTags({ symbol, assetType, className = '', onTagClick }: Props) {
  const context = useContext(StockSectorLabelsContext)
  if (!symbol || (assetType && assetType !== 'stock')) return null
  const key = symbol.trim().toUpperCase()
  const data = context?.data
  // 带交易所的代码必须精确匹配，禁止用裸码把指数 000001.SH 映射成平安银行。
  const canonical = key.includes('.') ? key : data?.aliases[key] ?? key
  const item = data?.stocks[canonical]
  if (data && !item && assetType !== 'stock') return null
  const day = context?.day ?? sectorLabelsToday()
  const prefix = data?.as_of === day ? '今日概念' : data?.as_of ? `${data.as_of} 概念` : '概念'
  const empty = context?.error ? '暂不可用' : !data ? '加载中' : '暂无数据'
  const title = data
    ? `概念取所属题材中综合热度最高者；行情日期 ${data.as_of ?? '暂无'}；更新 ${data.updated_at}${context?.error ? '；刷新失败，保留上次结果' : ''}`
    : context?.error ? '个股板块标签暂不可用，稍后自动重试' : '正在加载个股所属概念和行业'
  const tag = (kind: 'concept' | 'industry', label: string, text: string) => {
    const value = item?.[kind]
    const source = kind === 'concept' ? data?.concept_source : data?.industry_source
    const cls = `max-w-full whitespace-normal break-words rounded px-1 ${kind === 'concept' ? 'bg-orange-500/10 text-orange-600 dark:text-orange-400' : 'bg-sky-500/10 text-sky-600 dark:text-sky-400'}`
    return onTagClick && value && source
      ? <button type="button" className={cls} onClick={e => { e.stopPropagation(); onTagClick(kind, value, source) }}>{label}：{text}</button>
      : <span className={cls}>{label}：{text}</span>
  }
  return (
    <span data-stock-sector-tags={key} className={`inline-flex max-w-full flex-wrap items-center gap-1 align-middle font-sans text-[10px] font-normal leading-4 ${className}`} title={title}>
      {tag('concept', prefix, item?.concept ?? (data?.concept_source ? '待更新' : empty))}
      {tag('industry', '行业', item?.industry ?? empty)}
    </span>
  )
}
