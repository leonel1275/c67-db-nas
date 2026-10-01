package com.systutor.g5;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/** Arranca PostgreSQL + NAS al encender el telefono (BOOT_COMPLETED esta exento del limite de FGS en segundo plano). */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            return;
        }
        try {
            context.startForegroundService(new Intent(context, PgService.class));
        } catch (Throwable t) {
            Log.e(PgService.TAG, "no pude iniciar el servicio en boot: " + t);
        }
    }
}
