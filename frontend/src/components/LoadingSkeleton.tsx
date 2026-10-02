interface LoadingSkeletonProps {
  rows?: number
}

export function LoadingSkeleton({ rows = 5 }: LoadingSkeletonProps) {
  return (
    <div className="skeleton" role="status" aria-label="Loading">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="skeleton__row" />
      ))}
    </div>
  )
}
