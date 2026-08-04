import { useState, type ReactNode } from 'react'
import { Link as RouterLink, useLocation } from 'react-router-dom'
import {
  alpha,
  AppBar,
  Box,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Stack,
  Toolbar,
  Tooltip,
  Typography,
  useTheme,
} from '@mui/material'
import HubIcon from '@mui/icons-material/HubOutlined'
import DashboardIcon from '@mui/icons-material/SpaceDashboardOutlined'
import SmsIcon from '@mui/icons-material/ForumOutlined'
import DevicesIcon from '@mui/icons-material/RouterOutlined'
import SimIcon from '@mui/icons-material/SimCardOutlined'
import TaskIcon from '@mui/icons-material/EventRepeatOutlined'
import ConsoleIcon from '@mui/icons-material/TerminalOutlined'
import NotifyIcon from '@mui/icons-material/NotificationsActiveOutlined'
import LogsIcon from '@mui/icons-material/ReceiptLongOutlined'
import BackupIcon from '@mui/icons-material/BackupOutlined'
import SettingsIcon from '@mui/icons-material/SettingsOutlined'
import MenuIcon from '@mui/icons-material/Menu'
import LightIcon from '@mui/icons-material/LightModeOutlined'
import DarkIcon from '@mui/icons-material/DarkModeOutlined'
import LogoutIcon from '@mui/icons-material/LogoutOutlined'
import { VIZ, type Mode } from '../tokens'

const WIDTH = 224

const NAV = [
  { to: '/', label: '仪表盘', icon: <DashboardIcon /> },
  { to: '/messages', label: '短信', icon: <SmsIcon /> },
  { to: '/devices', label: '设备', icon: <DevicesIcon /> },
  { to: '/sims', label: 'SIM 卡', icon: <SimIcon /> },
  { to: '/tasks', label: '保号任务', icon: <TaskIcon /> },
  { to: '/console', label: 'AT 调试', icon: <ConsoleIcon /> },
  { to: '/notify', label: '通知', icon: <NotifyIcon /> },
  { to: '/logs', label: '日志', icon: <LogsIcon /> },
  { to: '/backup', label: '备份恢复', icon: <BackupIcon /> },
  { to: '/settings', label: '设置', icon: <SettingsIcon /> },
]

// Grouped so the scan path is 总览 → 通讯 → 系统, not ten equal rows.
const GROUPS: { label: string; items: typeof NAV }[] = [
  { label: '总览', items: NAV.slice(0, 1) },
  { label: '通讯', items: NAV.slice(1, 6) },
  { label: '系统', items: NAV.slice(6) },
]

interface Props {
  children: ReactNode
  mode: Mode
  onToggleMode: () => void
  onLogout: () => void
}

function Brand() {
  return (
    <Stack
      direction="row"
      spacing={1.5}
      alignItems="center"
      sx={{ minHeight: 48, px: 2, borderBottom: 1, borderColor: 'divider' }}
    >
      <Box
        aria-hidden
        sx={{
          width: 32,
          height: 32,
          borderRadius: 2,
          flexShrink: 0,
          display: 'grid',
          placeItems: 'center',
          bgcolor: 'primary.main',
          color: 'primary.contrastText',
        }}
      >
        <HubIcon sx={{ fontSize: 18 }} />
      </Box>
      <Box sx={{ minWidth: 0, lineHeight: 1.15 }}>
        <Typography
          sx={{ fontSize: '0.95rem', fontWeight: 700, letterSpacing: '-0.01em', whiteSpace: 'nowrap' }}
        >
          air780e hub
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.66rem' }}>
          SMS 自托管网关
        </Typography>
      </Box>
    </Stack>
  )
}

export function Layout({ children, mode, onToggleMode, onLogout }: Props) {
  const theme = useTheme()
  const location = useLocation()
  const [open, setOpen] = useState(false)

  const selectedTint = alpha(
    theme.palette.primary.main,
    theme.palette.mode === 'dark' ? 0.16 : 0.08,
  )

  const nav = (
    <Box sx={{ px: 1.25, pb: 2, overflowY: 'auto' }}>
      {GROUPS.map((group) => (
        <Box key={group.label}>
          <Typography
            variant="overline"
            sx={{
              display: 'block',
              px: 1.5,
              pt: 2,
              pb: 0.5,
              color: 'text.secondary',
              fontSize: '0.64rem',
              letterSpacing: '0.1em',
            }}
          >
            {group.label}
          </Typography>
          <List disablePadding>
            {group.items.map((item) => {
              const selected =
                item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to)
              return (
                <ListItemButton
                  key={item.to}
                  component={RouterLink}
                  to={item.to}
                  selected={selected}
                  onClick={() => setOpen(false)}
                  sx={{
                    borderRadius: 2,
                    mb: 0.25,
                    minHeight: 40,
                    '&.Mui-selected': {
                      bgcolor: selectedTint,
                      '& .MuiListItemIcon-root': { color: 'primary.main' },
                      '& .MuiListItemText-primary': { fontWeight: 700 },
                    },
                    '&.Mui-selected:hover': { bgcolor: selectedTint },
                  }}
                >
                  <ListItemIcon sx={{ minWidth: 36, color: 'text.secondary' }}>
                    {item.icon}
                  </ListItemIcon>
                  <ListItemText
                    primary={item.label}
                    primaryTypographyProps={{ fontSize: 14, fontWeight: 500 }}
                  />
                </ListItemButton>
              )
            })}
          </List>
        </Box>
      ))}
    </Box>
  )

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      {/* Translucent chrome: content scrolls under, the blur keeps text
          legible, and the surface reads as material rather than a bar
          (Apple §12). */}
      <AppBar
        position="fixed"
        color="default"
        elevation={0}
        sx={{
          zIndex: (t) => t.zIndex.drawer + 1,
          bgcolor: VIZ[mode].chrome,
          backdropFilter: 'blur(16px) saturate(160%)',
          WebkitBackdropFilter: 'blur(16px) saturate(160%)',
          borderBottom: 1,
          borderColor: 'divider',
          color: 'text.primary',
        }}
      >
        <Toolbar variant="dense">
          <IconButton
            edge="start"
            onClick={() => setOpen(!open)}
            sx={{ mr: 1, display: { sm: 'none' } }}
            aria-label="菜单"
          >
            <MenuIcon />
          </IconButton>
          <Typography
            variant="h3"
            sx={{ flexGrow: 1, display: { xs: 'block', sm: 'none' } }}
          >
            air780e hub
          </Typography>
          <Box sx={{ flexGrow: 1, display: { xs: 'none', sm: 'block' } }} />
          <Tooltip title={mode === 'dark' ? '切换到浅色' : '切换到深色'}>
            <IconButton onClick={onToggleMode} aria-label="切换主题">
              {mode === 'dark' ? <LightIcon /> : <DarkIcon />}
            </IconButton>
          </Tooltip>
          <Tooltip title="退出登录">
            <IconButton onClick={onLogout} aria-label="退出登录">
              <LogoutIcon />
            </IconButton>
          </Tooltip>
        </Toolbar>
      </AppBar>

      <Drawer
        variant="temporary"
        open={open}
        onClose={() => setOpen(false)}
        ModalProps={{ keepMounted: true }}
        sx={{
          display: { xs: 'block', sm: 'none' },
          '& .MuiDrawer-paper': { width: WIDTH, boxSizing: 'border-box' },
        }}
      >
        <Brand />
        {nav}
      </Drawer>

      <Drawer
        variant="permanent"
        sx={{
          width: WIDTH,
          flexShrink: 0,
          display: { xs: 'none', sm: 'block' },
          '& .MuiDrawer-paper': {
            width: WIDTH,
            boxSizing: 'border-box',
            borderRight: 1,
            borderColor: 'divider',
          },
        }}
      >
        <Brand />
        {nav}
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, p: { xs: 2, md: 3 }, width: 0 }}>
        <Toolbar variant="dense" />
        {children}
      </Box>
    </Box>
  )
}
