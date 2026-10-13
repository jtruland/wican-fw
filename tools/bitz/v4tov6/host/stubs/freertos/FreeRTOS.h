/* host stub: FreeRTOS types the v6 headers name (never called on host) */
#pragma once
#include <stdint.h>
typedef uint32_t EventBits_t;
typedef uint32_t TickType_t;
typedef int BaseType_t;
typedef unsigned UBaseType_t;
typedef void *QueueHandle_t;
typedef void *SemaphoreHandle_t;
typedef void *TaskHandle_t;
typedef void *EventGroupHandle_t;
typedef void *TimerHandle_t;
#define portMAX_DELAY 0xFFFFFFFFu
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#ifndef BIT0
#define BIT0 0x00000001u
#define BIT1 0x00000002u
#define BIT2 0x00000004u
#define BIT3 0x00000008u
#define BIT4 0x00000010u
#define BIT5 0x00000020u
#define BIT6 0x00000040u
#define BIT7 0x00000080u
#define BIT8 0x00000100u
#define BIT9 0x00000200u
#define BIT10 0x00000400u
#define BIT11 0x00000800u
#define BIT12 0x00001000u
#define BIT13 0x00002000u
#define BIT14 0x00004000u
#define BIT15 0x00008000u
#define BIT16 0x00010000u
#define BIT17 0x00020000u
#define BIT18 0x00040000u
#define BIT19 0x00080000u
#define BIT20 0x00100000u
#define BIT21 0x00200000u
#define BIT22 0x00400000u
#define BIT23 0x00800000u
#endif
