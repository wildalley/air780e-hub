import { useState, type ReactNode } from 'react'
import { Link as RouterLink, useLocation } from 'react-router-dom'
import {
  AppBar,
  Box,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Toolbar,
  Tooltip,
  Typography,
} from '@mui/material'
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
import type { Mode } from '../tokens'

const WIDTH = 216

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

interface Props {
  children: ReactNode
  mode: Mode
  onToggleMode: () => void
  onLogout: () => void
}

export function Layout({ children, mode, onToggleMode, onLogout }: Props) {
  const location = useLocation()
  const [open, setOpen] = useState(false)

  const nav = (
    <List sx={{ px: 1 }}>
      {NAV.map((item) => {
        const selected =
          item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to)
        return (
          <ListItemButton
            key={item.to}
            component={RouterLink}
            to={item.to}
            selected={selected}
            onClick={() => setOpen(false)}
            sx={{ borderRadius: 2, mb: 0.5 }}
          >
            <ListItemIcon sx={{ minWidth: 38 }}>{item.icon}</ListItemIcon>
            <ListItemText primaryTypographyProps={{ fontSize: 14 }} primary={item.label} />
          </ListItemButton>
        )
      })}
    </List>
  )

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      <AppBar
        position="fixed"
        color="default"
        elevation={0}
        sx={{
          zIndex: (theme) => theme.zIndex.drawer + 1,
          borderBottom: 1,
          borderColor: 'divider',
          bgcolor: 'background.paper',
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
          <Typography variant="h3" sx={{ flexGrow: 1 }}>
            air780e-hub
          </Typography>
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
        <Toolbar variant="dense" />
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
        <Toolbar variant="dense" />
        <Divider />
        {nav}
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, p: { xs: 2, md: 3 }, width: 0 }}>
        <Toolbar variant="dense" />
        {children}
      </Box>
    </Box>
  )
}
