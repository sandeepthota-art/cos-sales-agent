interface PaginationProps {
  page: number
  hasPrev: boolean
  hasNext: boolean
  onPrev: () => void
  onNext: () => void
}

export function Pagination({ page, hasPrev, hasNext, onPrev, onNext }: PaginationProps) {
  return (
    <div className="pagination">
      <button type="button" className="button button--secondary" onClick={onPrev} disabled={!hasPrev}>
        Previous
      </button>
      <span className="pagination__page">Page {page + 1}</span>
      <button type="button" className="button button--secondary" onClick={onNext} disabled={!hasNext}>
        Next
      </button>
    </div>
  )
}
