import { NavLink } from 'react-router-dom'

interface NavItem {
  to: string
  label: string
}

const NAV_SECTIONS: { title: string; items: NavItem[] }[] = [
  {
    title: 'Overview',
    items: [{ to: '/', label: 'Overview' }],
  },
  {
    title: 'Conversations',
    items: [
      { to: '/emails', label: 'Emails' },
      { to: '/threads', label: 'Threads' },
    ],
  },
  {
    title: 'Relationships',
    items: [
      { to: '/people', label: 'People' },
      { to: '/organizations', label: 'Organizations' },
    ],
  },
  {
    title: 'Pipeline',
    items: [
      { to: '/projects', label: 'Projects' },
      { to: '/opportunities', label: 'Opportunities' },
    ],
  },
  {
    title: 'Execution',
    items: [
      { to: '/commitments', label: 'Commitments' },
      { to: '/follow-ups', label: 'Follow-ups' },
      { to: '/meetings', label: 'Meetings' },
      { to: '/personal-items', label: 'Personal Items' },
      { to: '/knowledge', label: 'Knowledge' },
    ],
  },
  {
    title: 'Approvals',
    items: [
      { to: '/approvals/replies', label: 'Reply Approvals' },
      { to: '/approvals/calendar', label: 'Calendar Approvals' },
    ],
  },
]

export function Sidebar() {
  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="sidebar__brand">
        <span className="sidebar__brand-name">CoS Staff EA Agent</span>
        <span className="sidebar__brand-subtitle">Chief of Staff • Executive Assistant</span>
      </div>
      {NAV_SECTIONS.map((section) => (
        <div className="sidebar__section" key={section.title}>
          <p className="sidebar__section-title">{section.title}</p>
          {section.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                isActive ? 'sidebar__link sidebar__link--active' : 'sidebar__link'
              }
            >
              {item.label}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  )
}
